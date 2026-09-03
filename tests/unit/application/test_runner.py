"""The workflow runner: one execution from QUEUED to a terminal state (#102).

The runner sequences the steps and persists evidence; it decides nothing
the domain owns. Failures go through the #20 matrix and retry policy,
restarts resume from durable state, and every transition is the #14 graph
applied through the repository. Everything external is a fake here so the
sequencing itself is what is proved.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from esl_service.application.canonicalize import (
    CanonicalizationResult,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    BaselineReadResult,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadResult,
    WarehouseProvenance,
    WarehouseReadResult,
)
from esl_service.application.persist_run import (
    BaselineComparison,
    DiffCounts,
    PersistedRun,
    RunContext,
)
from esl_service.application.runner import (
    EVENT_RUN_RETRY_SCHEDULED,
    EVENT_STEP_RESUMED,
    RUN_STEPS,
    STEP_CANONICALIZE,
    STEP_DISCOVER,
    STEP_PERSIST,
    STEP_READ_STORE,
    TERMINAL_ACTIVE_MODE_UNSUPPORTED,
    TERMINAL_STORE_UNROUTABLE,
    StepFailure,
    WorkflowRunner,
)
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    RetryPolicy,
)
from esl_service.domain.outcomes import ExecutionMode, FailureClass
from esl_service.domain.workflow import ExecutionStatus, StepOutcome

WINDOW = SourceWindow(datetime(2026, 9, 2, 0, 0, tzinfo=UTC), datetime(2026, 9, 2, 0, 30, tzinfo=UTC))
STORE = StoreDirectoryEntry("084", "10.0.0.84", "PEPITO_084")
POLICY = RetryPolicy(
    max_attempts=3,
    timeout_seconds=Decimal(30),
    initial_backoff_seconds=Decimal(2),
    max_backoff_seconds=Decimal(60),
    jitter_ratio=Decimal("0.5"),
)


def provenance(instance: str, database: str, objects: tuple[str, ...]) -> WarehouseProvenance:
    return WarehouseProvenance(instance, database, objects, "test-v1", WINDOW.start, WINDOW.end, WINDOW.end)


# --- fakes -------------------------------------------------------------------------


@dataclass
class FakeExecution:
    id: UUID
    workflow_name: str = "esl-refresh"
    store_code: str = "084"
    mode: str = "SHADOW"
    status: str = "QUEUED"
    source_window_start: datetime = WINDOW.start
    source_window_end: datetime = WINDOW.end
    configuration_version_id: UUID = field(default_factory=uuid4)
    rule_version: str = "compatibility-v1"
    correlation_id: UUID = field(default_factory=uuid4)
    retry_not_before: datetime | None = None


@dataclass
class FakeStep:
    id: UUID
    step_name: str
    attempt: int
    outcome: str
    failure_class: str | None = None
    checkpoints: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class FakeCheckpoint:
    checkpoint_key: str
    watermark: str
    payload: dict[str, object]


@dataclass
class FakeExecutions:
    executions: dict[UUID, FakeExecution] = field(default_factory=dict)
    steps: list[FakeStep] = field(default_factory=list)
    events: list[tuple[UUID, str, dict[str, object]]] = field(default_factory=list)
    transitions: list[tuple[str, str]] = field(default_factory=list)
    heartbeats: int = 0
    released: list[str] = field(default_factory=list)

    def add(self, execution: FakeExecution) -> FakeExecution:
        self.executions[execution.id] = execution
        return execution

    def get_execution(self, execution_id: UUID) -> FakeExecution:
        return self.executions[execution_id]

    def transition_execution(
        self, execution_id: UUID, expected: ExecutionStatus, requested: ExecutionStatus, *,
        terminal_reason: str | None = None, retry_not_before: datetime | None = None,
    ) -> FakeExecution:
        execution = self.executions[execution_id]
        assert execution.status == expected.value, (execution.status, expected)
        from esl_service.domain.workflow import transition_execution

        transition_execution(expected, requested)  # the #14 graph decides
        execution.status = requested.value
        # Mirrors the repository: a due time belongs to RETRY_WAIT only.
        execution.retry_not_before = retry_not_before if requested is ExecutionStatus.RETRY_WAIT else None
        self.transitions.append((expected.value, requested.value))
        if terminal_reason is not None:
            self.terminal_reason = terminal_reason
        return execution

    def start_step(self, execution_id: UUID, step_name: str, *, attempt: int = 1) -> FakeStep:
        step = FakeStep(uuid4(), step_name, attempt, "RUNNING")
        self.steps.append(step)
        return step

    def finish_step(self, step_id: UUID, *, outcome: str, failure_class: FailureClass | None = None) -> FakeStep:
        step = next(s for s in self.steps if s.id == step_id)
        step.outcome = outcome
        step.failure_class = None if failure_class is None else failure_class.value
        return step

    def append_checkpoint(self, step_id: UUID, *, checkpoint_key: str, checkpoint_version: int, watermark: str, payload: dict[str, object], payload_schema_version: str = "checkpoint-v1", payload_hash: str | None = None) -> FakeCheckpoint:
        step = next(s for s in self.steps if s.id == step_id)
        checkpoint = FakeCheckpoint(checkpoint_key, watermark, dict(payload))
        step.checkpoints.append(checkpoint)
        return checkpoint

    def append_event(self, execution_id: UUID, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((execution_id, event_type, dict(payload)))

    def step_history(self, execution_id: UUID) -> Sequence[FakeStep]:
        return list(self.steps)

    def configuration_hash_of(self, configuration_version_id: UUID) -> str:
        return "c" * 64

    def heartbeat_scope(self, scope_key: str, execution_id: UUID) -> bool:
        self.heartbeats += 1
        return True

    def release_scope(self, scope_key: str, execution_id: UUID) -> bool:
        self.released.append(scope_key)
        return True

    def recoverable_executions(self) -> Sequence[FakeExecution]:
        return [e for e in self.executions.values() if e.status in ("QUEUED", "RUNNING", "RETRY_WAIT", "RECOVERING")]


@dataclass
class FakeSources:
    routable: bool = True
    fail_store_read: FailureSignal | None = None
    crash_store_read: Exception | None = None
    baseline: BaselineReadResult | None = None
    calls: list[str] = field(default_factory=list)

    def discover_store(self, store_code: str, window: SourceWindow) -> StoreDirectoryEntry | None:
        self.calls.append("discover")
        return STORE if self.routable else None

    def read_warehouse(self, store_code: str, window: SourceWindow) -> WarehouseReadResult:
        self.calls.append("read-warehouse")
        return WarehouseReadResult((), (), provenance("sql.internal", "DBWH_8555", ("dbo.DimItemMapping", "dbo.FactCampaign")))

    def read_uom_mappings(self, item_codes: Sequence[str], window: SourceWindow) -> UomMappingReadResult:
        self.calls.append("read-pepito-ho")
        return UomMappingReadResult((), provenance("192.168.85.18", "PEPITO_HO", ("dbo.ITEM_UOM_MAPPING_MST",)))

    def read_store(self, entry: StoreDirectoryEntry, window: SourceWindow) -> StoreReadResult:
        self.calls.append("read-store")
        if self.fail_store_read is not None:
            raise StepFailure(STEP_READ_STORE, self.fail_store_read)
        if self.crash_store_read is not None:
            raise self.crash_store_read
        rows = {name: () for name in STORE_OBJECTS}
        rows["dbo.ITEM_MST"] = ({"ITM_CD": "SKU-1", "ITM_STATUS": "O", "ITM_SALES_UOM": "PCS"},)
        return StoreReadResult.from_mapping(rows, provenance(entry.org_ip, entry.org_db, STORE_OBJECTS))

    def read_baseline(self, store_code: str, window: SourceWindow) -> BaselineReadResult | None:
        self.calls.append("read-baseline")
        return self.baseline


@dataclass
class FakePersist:
    calls: list[dict[str, Any]] = field(default_factory=list)
    resumed: bool = False
    baseline: BaselineComparison | None = None

    def __call__(self, result: CanonicalizationResult, context: RunContext, *, legacy_baseline: BaselineReadResult | None = None, step_id: UUID | None = None, **_: Any) -> PersistedRun:
        self.calls.append({"context": context, "legacy_baseline": legacy_baseline, "step_id": step_id, "records": len(result.records)})
        return PersistedRun(uuid4(), uuid4(), (), (), DiffCounts(len(result.records), 0, 0, 0), self.baseline, self.resumed)


@dataclass
class Harness:
    executions: FakeExecutions
    sources: FakeSources
    persist: FakePersist
    runner: WorkflowRunner
    canonicalize_calls: list[dict[str, Any]]


def build(*, sources: FakeSources | None = None, persist: FakePersist | None = None) -> Harness:
    executions = FakeExecutions()
    sources = sources or FakeSources()
    persist = persist or FakePersist()
    canonicalize_calls: list[dict[str, Any]] = []

    from esl_service.application.canonicalize import canonicalize_store

    def canonicalize(bundle: Any, *, reference_time: datetime, configuration_version: str, rule_version: str) -> CanonicalizationResult:
        canonicalize_calls.append({"reference_time": reference_time, "configuration_version": configuration_version, "rule_version": rule_version})
        return canonicalize_store(bundle, reference_time=reference_time, configuration_version=configuration_version, rule_version=rule_version)

    runner = WorkflowRunner(
        executions=executions,
        sources=sources,
        retry_policy=POLICY,
        canonicalize=canonicalize,
        persist=persist,
        clock=lambda: datetime(2026, 9, 2, 0, 31, tzinfo=UTC),
        jitter=lambda: 0.0,
        store_timezone="Asia/Jakarta",
    )
    return Harness(executions, sources, persist, runner, canonicalize_calls)


def queued(harness: Harness, **overrides: Any) -> FakeExecution:
    return harness.executions.add(FakeExecution(uuid4(), **overrides))


# --- the happy path -----------------------------------------------------------------


def test_a_queued_shadow_execution_runs_every_step_and_succeeds() -> None:
    harness = build()
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert harness.executions.transitions == [("QUEUED", "RUNNING"), ("RUNNING", "SUCCEEDED")]
    assert [s.step_name for s in harness.executions.steps] == list(RUN_STEPS)
    assert all(s.outcome == StepOutcome.SUCCEEDED.value for s in harness.executions.steps)
    assert all(s.checkpoints and s.checkpoints[-1].checkpoint_key == f"{s.step_name}:done" for s in harness.executions.steps)
    assert harness.executions.heartbeats == len(RUN_STEPS)
    assert harness.executions.released == ["esl-refresh:084"]
    # store before PEPITO_HO: the UOM read is bounded by the store's item set (#93)
    assert harness.sources.calls == ["discover", "read-warehouse", "read-store", "read-pepito-ho", "read-baseline"]
    (persist_call,) = harness.persist.calls
    assert persist_call["context"].mode is ExecutionMode.SHADOW
    assert persist_call["context"].execution_id == execution.id
    assert persist_call["context"].source_window == WINDOW
    assert persist_call["step_id"] is not None


def test_the_reference_time_is_the_window_end_in_the_store_timezone() -> None:
    harness = build()
    execution = queued(harness)

    harness.runner.run(execution.id)

    (call,) = harness.canonicalize_calls
    assert call["reference_time"] == WINDOW.end.astimezone(ZoneInfo("Asia/Jakarta"))
    assert call["configuration_version"] == str(execution.configuration_version_id)
    assert call["rule_version"] == "compatibility-v1"


def test_the_baseline_is_passed_to_persist_when_the_source_has_one() -> None:
    legacy = BaselineReadResult((), provenance("sql.internal", "ESL", ("dbo.tb_ESL",)))
    harness = build(sources=FakeSources(baseline=legacy))
    execution = queued(harness)

    harness.runner.run(execution.id)

    assert harness.persist.calls[0]["legacy_baseline"] is legacy


def test_exceptions_in_the_persisted_run_end_in_succeeded_with_exceptions() -> None:
    harness = build(persist=FakePersist(baseline=BaselineComparison(compared=1, mismatched=1, missing_in_legacy=0, only_in_legacy=0)))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS


# --- refusals before any step ----------------------------------------------------------


def test_active_mode_fails_terminally_before_any_step() -> None:
    harness = build()
    execution = queued(harness, mode="ACTIVE")

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.terminal_reason == TERMINAL_ACTIVE_MODE_UNSUPPORTED
    assert harness.executions.steps == []
    assert harness.executions.released == ["esl-refresh:084"]


def test_an_unroutable_store_fails_terminally_at_discover() -> None:
    harness = build(sources=FakeSources(routable=False))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.terminal_reason.startswith(TERMINAL_STORE_UNROUTABLE)
    (step,) = harness.executions.steps
    assert (step.step_name, step.outcome, step.failure_class) == (STEP_DISCOVER, "FAILED", FailureClass.NON_RETRYABLE.value)


def test_a_terminal_execution_is_left_alone() -> None:
    harness = build()
    execution = queued(harness, status="SUCCEEDED")

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert harness.executions.transitions == [] and harness.executions.steps == []


# --- failures through the #20 matrix ---------------------------------------------------


def test_a_retryable_failure_schedules_a_bounded_retry() -> None:
    harness = build(sources=FakeSources(fail_store_read=FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.RETRY_WAIT
    assert outcome.retry_after_seconds == POLICY.delay_for(1, jitter=0.0)
    failed = harness.executions.steps[-1]
    assert (failed.step_name, failed.outcome, failed.failure_class) == (STEP_READ_STORE, "FAILED", "RETRYABLE")
    assert harness.executions.transitions[-1] == ("RUNNING", "RETRY_WAIT")
    assert harness.executions.released == []  # the scope stays owned while a retry is pending
    events = [e for e in harness.executions.events if e[1] == EVENT_RUN_RETRY_SCHEDULED]
    assert events and events[0][2]["step"] == STEP_READ_STORE and events[0][2]["attempt"] == 1


def test_a_retryable_failure_records_when_the_retry_is_due() -> None:
    """The due time is durable state, so the delay survives a process restart (0008)."""

    harness = build(sources=FakeSources(fail_store_read=FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.retry_after_seconds is not None
    expected = datetime(2026, 9, 2, 0, 31, tzinfo=UTC) + timedelta(seconds=float(outcome.retry_after_seconds))
    assert harness.executions.executions[execution.id].retry_not_before == expected


def test_a_retry_resumes_and_repeats_only_the_failed_step_onwards() -> None:
    sources = FakeSources(fail_store_read=FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE))
    harness = build(sources=sources)
    execution = queued(harness)
    harness.runner.run(execution.id)
    sources.fail_store_read = None
    sources.calls.clear()

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert harness.executions.transitions[-2:] == [("RETRY_WAIT", "RUNNING"), ("RUNNING", "SUCCEEDED")]
    assert harness.executions.executions[execution.id].retry_not_before is None
    read_store_attempts = [s.attempt for s in harness.executions.steps if s.step_name == STEP_READ_STORE]
    assert read_store_attempts == [1, 2]
    assert "discover" not in sources.calls  # the discovered entry is in the checkpoint
    resumed = [e for e in harness.executions.events if e[1] == EVENT_STEP_RESUMED]
    assert [e[2]["step"] for e in resumed] == [STEP_DISCOVER]


def test_retries_are_exhausted_into_a_terminal_failure() -> None:
    sources = FakeSources(fail_store_read=FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE))
    harness = build(sources=sources)
    execution = queued(harness)

    statuses = [harness.runner.run(execution.id).status for _ in range(POLICY.max_attempts)]

    assert statuses == [ExecutionStatus.RETRY_WAIT, ExecutionStatus.RETRY_WAIT, ExecutionStatus.FAILED]
    assert harness.executions.terminal_reason == f"{STEP_READ_STORE}:sql_server:unavailable:RETRYABLE:attempts_exhausted"
    assert harness.executions.released == ["esl-refresh:084"]


def test_a_non_retryable_failure_is_terminal_at_once() -> None:
    harness = build(sources=FakeSources(fail_store_read=FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.terminal_reason == f"{STEP_READ_STORE}:source_data:malformed:NON_RETRYABLE"
    assert harness.executions.steps[-1].failure_class == "NON_RETRYABLE"


def test_an_unexpected_exception_is_operator_action_required_and_never_retried() -> None:
    harness = build(sources=FakeSources(crash_store_read=RuntimeError("tcp://10.0.0.84?password=secret")))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.terminal_reason == f"{STEP_READ_STORE}:unexpected:RuntimeError:OPERATOR_ACTION_REQUIRED"
    assert "secret" not in repr(harness.executions.events) and "://" not in repr(harness.executions.events)
    assert harness.executions.steps[-1].failure_class == "OPERATOR_ACTION_REQUIRED"


# --- restart recovery (#18) -----------------------------------------------------------------


def test_recover_all_marks_interrupted_running_executions_for_recovery() -> None:
    harness = build()
    interrupted = queued(harness, status="RUNNING")
    waiting = queued(harness, status="RETRY_WAIT")
    fresh = queued(harness)

    recovered = harness.runner.recover_all()

    assert recovered == (interrupted.id,)
    assert harness.executions.executions[interrupted.id].status == "RECOVERING"
    assert harness.executions.executions[waiting.id].status == "RETRY_WAIT"
    assert harness.executions.executions[fresh.id].status == "QUEUED"


def test_a_recovering_execution_resumes_from_its_checkpoints() -> None:
    harness = build(persist=FakePersist(resumed=True))
    execution = queued(harness)
    # Simulate the earlier attempt: discover done, then the process died.
    step = harness.executions.start_step(execution.id, STEP_DISCOVER, attempt=1)
    harness.executions.finish_step(step.id, outcome="SUCCEEDED")
    harness.executions.append_checkpoint(step.id, checkpoint_key=f"{STEP_DISCOVER}:done", checkpoint_version=1, watermark="084", payload={"store_code": "084", "org_ip": "10.0.0.84", "org_db": "PEPITO_084"})
    execution.status = "RECOVERING"

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert harness.executions.transitions == [("RECOVERING", "RUNNING"), ("RUNNING", "SUCCEEDED")]
    assert "discover" not in harness.sources.calls
    assert [s.step_name for s in harness.executions.steps][1:] == list(RUN_STEPS[1:])
    assert harness.persist.calls[0]["step_id"] is not None


def test_a_running_execution_found_by_the_worker_is_recovered_first() -> None:
    """The process died mid-run; the graph allows RUNNING -> RECOVERING -> RUNNING only."""

    harness = build()
    execution = queued(harness, status="RUNNING")

    outcome = harness.runner.run(execution.id)

    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert harness.executions.transitions[:2] == [("RUNNING", "RECOVERING"), ("RECOVERING", "RUNNING")]


def test_the_persist_step_reports_when_it_resumed_from_durable_state() -> None:
    harness = build(persist=FakePersist(resumed=True))
    execution = queued(harness)

    outcome = harness.runner.run(execution.id)

    assert outcome.persisted is not None and outcome.persisted.resumed is True
    persist_step = next(s for s in harness.executions.steps if s.step_name == STEP_PERSIST)
    assert persist_step.checkpoints[-1].payload["resumed"] is True


def test_canonicalize_records_the_counts_in_its_checkpoint() -> None:
    harness = build()
    execution = queued(harness)

    harness.runner.run(execution.id)

    step = next(s for s in harness.executions.steps if s.step_name == STEP_CANONICALIZE)
    assert step.checkpoints[-1].payload["extracted"] == 1
    assert step.checkpoints[-1].payload["records"] == 1
