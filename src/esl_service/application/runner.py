"""The workflow runner: one execution from QUEUED to a terminal state (#102).

Every part of a run existed except the thing that runs it. The runner is
the application-layer service that takes one execution and drives it
through the approved state graph (#14) step by step, with a durable
checkpoint after each step (#18), the #20 classification matrix and retry
policy on failure, and the scope lease (#17) heartbeated for the life of the
run and released at its end. It sequences and records; it decides nothing
the domain owns: canonicalization is #103, persistence and reconciliation
are #104, and both are injected.

Steps, in order: ``discover`` (the store's server from ``DimStore``, #91),
``read-warehouse``, ``read-pepito-ho``, ``read-store`` (#91 to #93),
``canonicalize`` (#103), ``persist`` (#104, which finalizes the report, so
reconciliation is inside it rather than a step of its own).

Restart: a step that finished is not repeated when its result is durable.
The discovered store entry lives in its checkpoint and the persisted run in
the state store, so both resume; the raw reads are deliberately not stored
(AD-005 keeps raw rows out of the state store), so a resume repeats them at
the next attempt number, which is visible in ``execution_step``. Every
failure is recorded by classification and step, never by driver text.
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from esl_service.application.canonicalize import (
    CanonicalizationResult,
    StoreSourceBundle,
    canonicalize_store,
)
from esl_service.application.contracts import (
    BaselineReadResult,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadResult,
    WarehouseProvenance,
    WarehouseReadResult,
)
from esl_service.application.persist_run import PersistedRun, RunContext, persist_run
from esl_service.domain.failures import (
    FailureSignal,
    RetryPolicy,
    UnclassifiedFailure,
    classify,
)
from esl_service.domain.outcomes import ExecutionMode, FailureClass
from esl_service.domain.ownership import scope_key
from esl_service.domain.workflow import ExecutionStatus, StepOutcome, is_terminal

STEP_DISCOVER = "discover"
STEP_READ_WAREHOUSE = "read-warehouse"
STEP_READ_PEPITO_HO = "read-pepito-ho"
STEP_READ_STORE = "read-store"
STEP_CANONICALIZE = "canonicalize"
STEP_PERSIST = "persist"
#: Store before PEPITO_HO: the UOM read is bounded by the store's item set
#: (#93), which only the store read supplies, as in the procedure.
RUN_STEPS: tuple[str, ...] = (
    STEP_DISCOVER,
    STEP_READ_WAREHOUSE,
    STEP_READ_STORE,
    STEP_READ_PEPITO_HO,
    STEP_CANONICALIZE,
    STEP_PERSIST,
)

TERMINAL_ACTIVE_MODE_UNSUPPORTED = "ACTIVE_MODE_UNSUPPORTED"
TERMINAL_STORE_UNROUTABLE = "STORE_UNROUTABLE"

EVENT_RUN_RETRY_SCHEDULED = "RUN_RETRY_SCHEDULED"
EVENT_RUN_RECOVERY_MARKED = "RUN_RECOVERY_MARKED"
EVENT_STEP_RESUMED = "STEP_RESUMED_FROM_CHECKPOINT"
EVENT_STEP_FAILED = "STEP_FAILED"

_DONE = ":done"


class StepFailure(Exception):
    """A step failed with a classified signal; the runner decides retry or terminal."""

    def __init__(self, step: str, signal: FailureSignal) -> None:
        super().__init__(f"{step} failed: {signal.dependency.value.lower()} {signal.kind.value.lower()}")
        self.step = step
        self.signal = signal


# --- ports --------------------------------------------------------------------------


class ExecutionRow(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def workflow_name(self) -> str: ...

    @property
    def store_code(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def source_window_start(self) -> datetime: ...

    @property
    def source_window_end(self) -> datetime: ...

    @property
    def configuration_version_id(self) -> UUID: ...

    @property
    def rule_version(self) -> str: ...


class CheckpointRow(Protocol):
    @property
    def checkpoint_key(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object]: ...


class StepRow(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def step_name(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def outcome(self) -> str: ...

    @property
    def checkpoints(self) -> Sequence[CheckpointRow]: ...


class RunnerExecutionPort(Protocol):
    """The ``ExecutionRepository`` methods the runner uses."""

    def get_execution(self, execution_id: UUID) -> ExecutionRow: ...

    def transition_execution(
        self,
        execution_id: UUID,
        expected_status: ExecutionStatus,
        requested_status: ExecutionStatus,
        *,
        terminal_reason: str | None = None,
    ) -> object: ...

    def start_step(self, execution_id: UUID, step_name: str, *, attempt: int = 1) -> StepRow: ...

    def finish_step(
        self, step_id: UUID, *, outcome: str, failure_class: FailureClass | None = None
    ) -> object: ...

    def append_checkpoint(
        self,
        step_id: UUID,
        *,
        checkpoint_key: str,
        checkpoint_version: int,
        watermark: str,
        payload: Mapping[str, object],
        payload_schema_version: str = "checkpoint-v1",
        payload_hash: str | None = None,
    ) -> object: ...

    def append_event(self, execution_id: UUID, event_type: str, payload: Mapping[str, object]) -> object: ...

    def step_history(self, execution_id: UUID) -> Sequence[StepRow]: ...

    def configuration_hash_of(self, configuration_version_id: UUID) -> str: ...

    def heartbeat_scope(self, scope_key: str, execution_id: UUID) -> bool: ...

    def release_scope(self, scope_key: str, execution_id: UUID) -> bool: ...

    def recoverable_executions(self) -> Sequence[ExecutionRow]: ...


class SourcePort(Protocol):
    """The four tiers, already behind their adapters (#91 to #94)."""

    def discover_store(self, store_code: str, window: SourceWindow) -> StoreDirectoryEntry | None: ...

    def read_warehouse(self, store_code: str, window: SourceWindow) -> WarehouseReadResult: ...

    def read_uom_mappings(self, item_codes: Sequence[str], window: SourceWindow) -> UomMappingReadResult: ...

    def read_store(self, entry: StoreDirectoryEntry, window: SourceWindow) -> StoreReadResult: ...

    def read_baseline(self, store_code: str, window: SourceWindow) -> BaselineReadResult | None: ...


Canonicalizer = Callable[..., CanonicalizationResult]
Persister = Callable[..., PersistedRun]


# --- outcome --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutcome:
    execution_id: UUID
    status: ExecutionStatus
    terminal_reason: str | None
    steps: tuple[str, ...]
    skipped_steps: tuple[str, ...]
    retry_after_seconds: Decimal | None
    persisted: PersistedRun | None


@dataclass
class _RunState:
    entry: StoreDirectoryEntry | None = None
    warehouse: WarehouseReadResult | None = None
    uom: UomMappingReadResult | None = None
    store_rows: StoreReadResult | None = None
    canonical: CanonicalizationResult | None = None
    baseline: BaselineReadResult | None = None
    persisted: PersistedRun | None = None


class _Stop(Exception):
    """Internal: the run reached a non-success end; carries the outcome."""

    def __init__(self, outcome: RunOutcome) -> None:
        super().__init__(outcome.status.value)
        self.outcome = outcome


# --- the runner -----------------------------------------------------------------------


class WorkflowRunner:
    """Drives one execution through the approved graph, step by step."""

    def __init__(
        self,
        *,
        executions: RunnerExecutionPort,
        sources: SourcePort,
        retry_policy: RetryPolicy,
        canonicalize: Canonicalizer = canonicalize_store,
        persist: Persister = persist_run,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = random.random,
        store_timezone: str = "Asia/Jakarta",
    ) -> None:
        self._executions = executions
        self._sources = sources
        self._policy = retry_policy
        self._canonicalize = canonicalize
        self._persist = persist
        self._clock = clock
        self._jitter = jitter
        self._zone = ZoneInfo(store_timezone)

    # -- restart recovery (#18) -------------------------------------------------

    def recover_all(self) -> tuple[UUID, ...]:
        """Mark every execution the previous process left RUNNING as RECOVERING.

        The graph allows ``RUNNING -> RECOVERING -> RUNNING`` only; the worker
        then resumes each one from its checkpoints. QUEUED and RETRY_WAIT
        executions need no marking: they are runnable as they are.
        """

        marked: list[UUID] = []
        for execution in self._executions.recoverable_executions():
            if execution.status == ExecutionStatus.RUNNING.value:
                self._executions.transition_execution(
                    execution.id, ExecutionStatus.RUNNING, ExecutionStatus.RECOVERING
                )
                self._executions.append_event(
                    execution.id, EVENT_RUN_RECOVERY_MARKED, {"reason": "process restarted"}
                )
                marked.append(execution.id)
        return tuple(marked)

    # -- one run -----------------------------------------------------------------------

    def run(self, execution_id: UUID) -> RunOutcome:
        execution = self._executions.get_execution(execution_id)
        status = ExecutionStatus(execution.status)
        if is_terminal(status):
            return RunOutcome(execution_id, status, None, (), (), None, None)

        if status is ExecutionStatus.RUNNING:
            # Found mid-run by a fresh process: recover, then resume.
            self._executions.transition_execution(execution_id, status, ExecutionStatus.RECOVERING)
            self._executions.append_event(execution_id, EVENT_RUN_RECOVERY_MARKED, {"reason": "found running"})
            status = ExecutionStatus.RECOVERING
        self._executions.transition_execution(execution_id, status, ExecutionStatus.RUNNING)

        scope = scope_key(execution.workflow_name, execution.store_code)
        if ExecutionMode(execution.mode) is not ExecutionMode.SHADOW:
            return self._fail(execution_id, scope, TERMINAL_ACTIVE_MODE_UNSUPPORTED, (), ())

        window = SourceWindow(execution.source_window_start, execution.source_window_end)
        state = _RunState()
        history = {step.step_name: step for step in self._executions.step_history(execution_id)}
        executed: list[str] = []
        skipped: list[str] = []

        try:
            for step_name in RUN_STEPS:
                prior = history.get(step_name)
                attempt = (prior.attempt if prior is not None else 0) + 1
                if prior is not None and prior.outcome == StepOutcome.SUCCEEDED.value and self._restore(step_name, prior, state):
                    self._executions.append_event(execution_id, EVENT_STEP_RESUMED, {"step": step_name, "attempt": prior.attempt})
                    skipped.append(step_name)
                    continue
                self._executions.heartbeat_scope(scope, execution_id)
                self._run_step(execution, window, scope, step_name, attempt, state)
                executed.append(step_name)
        except _Stop as stop:
            return stop.outcome

        final = ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS if self._had_exceptions(state) else ExecutionStatus.SUCCEEDED
        self._executions.transition_execution(execution_id, ExecutionStatus.RUNNING, final)
        self._executions.release_scope(scope, execution_id)
        return RunOutcome(execution_id, final, None, tuple(executed), tuple(skipped), None, state.persisted)

    # -- steps ---------------------------------------------------------------------------

    def _run_step(
        self,
        execution: ExecutionRow,
        window: SourceWindow,
        scope: str,
        step_name: str,
        attempt: int,
        state: _RunState,
    ) -> None:
        step = self._executions.start_step(execution.id, step_name, attempt=attempt)
        try:
            payload, watermark = self._execute(execution, window, step_name, step.id, state)
        except StepFailure as failure:
            self._handle_failure(
                execution.id, scope, step, step_name, attempt, failure.signal, None,
                reason_override=getattr(failure, "reason_override", None),
            )
        except Exception as error:  # noqa: BLE001 - classified below, never re-raised with its text
            signal = getattr(error, "signal", None)
            if isinstance(signal, FailureSignal):
                self._handle_failure(execution.id, scope, step, step_name, attempt, signal, None)
            else:
                self._handle_failure(execution.id, scope, step, step_name, attempt, None, type(error).__name__)
        else:
            self._executions.append_checkpoint(
                step.id,
                checkpoint_key=f"{step_name}{_DONE}",
                checkpoint_version=1,
                watermark=watermark,
                payload=payload,
            )
            self._executions.finish_step(step.id, outcome=StepOutcome.SUCCEEDED.value)

    def _execute(
        self, execution: ExecutionRow, window: SourceWindow, step_name: str, step_id: UUID, state: _RunState
    ) -> tuple[dict[str, object], str]:
        store_code = execution.store_code
        if step_name == STEP_DISCOVER:
            entry = self._sources.discover_store(store_code, window)
            if entry is None:
                raise _Unroutable(store_code)
            state.entry = entry
            return {"store_code": entry.store_code, "org_ip": entry.org_ip, "org_db": entry.org_db}, store_code
        if step_name == STEP_READ_WAREHOUSE:
            state.warehouse = self._sources.read_warehouse(store_code, window)
            return _provenance_payload(state.warehouse.provenance), _watermark(state.warehouse.provenance)
        if step_name == STEP_READ_STORE:
            assert state.entry is not None
            state.store_rows = self._sources.read_store(state.entry, window)
            return _provenance_payload(state.store_rows.provenance), _watermark(state.store_rows.provenance)
        if step_name == STEP_READ_PEPITO_HO:
            # The item set comes from the store's item master, so this step
            # runs after read-store (RUN_STEPS order), as in the procedure.
            assert state.store_rows is not None
            codes = tuple(dict.fromkeys(_text(row.get("ITM_CD")) for row in state.store_rows.items if _text(row.get("ITM_CD"))))
            state.uom = self._sources.read_uom_mappings(codes, window) if codes else UomMappingReadResult((), state.store_rows.provenance)
            return {**_provenance_payload(state.uom.provenance), "item_codes": len(codes)}, _watermark(state.uom.provenance)
        if step_name == STEP_CANONICALIZE:
            assert state.entry is not None and state.warehouse is not None and state.store_rows is not None and state.uom is not None
            bundle = StoreSourceBundle(state.entry, state.warehouse, state.store_rows, state.uom)
            state.canonical = self._canonicalize(
                bundle,
                reference_time=execution.source_window_end.astimezone(self._zone),
                configuration_version=str(execution.configuration_version_id),
                rule_version=execution.rule_version,
            )
            counts = state.canonical.counts
            return (
                {"records": len(state.canonical.records), "extracted": counts.extracted, "rejected": counts.rejected, "unresolved": counts.unresolved, "issues": len(state.canonical.issues)},
                _watermark(state.store_rows.provenance),
            )
        if step_name == STEP_PERSIST:
            assert state.canonical is not None
            state.baseline = self._sources.read_baseline(store_code, window)
            context = RunContext(
                execution_id=execution.id,
                store_code=store_code,
                mode=ExecutionMode(execution.mode),
                configuration_hash=self._executions.configuration_hash_of(execution.configuration_version_id),
                source_window=window,
                rule_version=execution.rule_version,
            )
            state.persisted = self._persist(state.canonical, context, legacy_baseline=state.baseline, step_id=step_id)
            persisted = state.persisted
            return (
                {"snapshot_set_id": str(persisted.snapshot_set_id), "report_id": str(persisted.report_id), "resumed": persisted.resumed, "intended": len(persisted.action_ids)},
                str(persisted.snapshot_set_id),
            )
        raise ValueError(f"unknown step {step_name!r}")

    def _restore(self, step_name: str, prior: StepRow, state: _RunState) -> bool:
        """Restore a completed step's durable result; reads are not durable."""

        if step_name != STEP_DISCOVER:
            return False
        for checkpoint in prior.checkpoints:
            if checkpoint.checkpoint_key == f"{STEP_DISCOVER}{_DONE}":
                payload = checkpoint.payload
                state.entry = StoreDirectoryEntry(
                    _text(payload.get("store_code")), _text(payload.get("org_ip")), _text(payload.get("org_db"))
                )
                return True
        return False

    # -- failure handling (#20) ------------------------------------------------------------

    def _handle_failure(
        self,
        execution_id: UUID,
        scope: str,
        step: StepRow,
        step_name: str,
        attempt: int,
        signal: FailureSignal | None,
        unexpected: str | None,
        *,
        reason_override: str | None = None,
    ) -> None:
        if signal is not None:
            try:
                failure_class = classify(signal)
            except UnclassifiedFailure:
                failure_class = FailureClass.OPERATOR_ACTION_REQUIRED
            reason = f"{step_name}:{signal.dependency.value.lower()}:{signal.kind.value.lower()}:{failure_class.value}"
            if reason_override is not None:
                reason = f"{reason_override}:{failure_class.value}"
        else:
            failure_class = FailureClass.OPERATOR_ACTION_REQUIRED
            reason = f"{step_name}:unexpected:{unexpected}:{failure_class.value}"
        self._executions.finish_step(step.id, outcome=StepOutcome.FAILED.value, failure_class=failure_class)
        self._executions.append_event(
            execution_id, EVENT_STEP_FAILED, {"step": step_name, "attempt": attempt, "failure_class": failure_class.value, "reason": reason}
        )
        if self._policy.should_retry(failure_class, attempt):
            delay = self._policy.delay_for(attempt, jitter=self._jitter())
            self._executions.transition_execution(execution_id, ExecutionStatus.RUNNING, ExecutionStatus.RETRY_WAIT)
            self._executions.append_event(
                execution_id, EVENT_RUN_RETRY_SCHEDULED, {"step": step_name, "attempt": attempt, "retry_after_seconds": str(delay)}
            )
            raise _Stop(RunOutcome(execution_id, ExecutionStatus.RETRY_WAIT, None, (), (), delay, None))
        if failure_class is FailureClass.RETRYABLE:
            reason += ":attempts_exhausted"
        raise _Stop(self._fail(execution_id, scope, reason, (), ()))

    def _fail(self, execution_id: UUID, scope: str, reason: str, executed: tuple[str, ...], skipped: tuple[str, ...]) -> RunOutcome:
        self._executions.transition_execution(
            execution_id, ExecutionStatus.RUNNING, ExecutionStatus.FAILED, terminal_reason=reason
        )
        self._executions.release_scope(scope, execution_id)
        return RunOutcome(execution_id, ExecutionStatus.FAILED, reason, executed, skipped, None, None)

    @staticmethod
    def _had_exceptions(state: _RunState) -> bool:
        counts = state.canonical.counts if state.canonical is not None else None
        baseline = state.persisted.baseline if state.persisted is not None else None
        return bool(
            (counts is not None and (counts.rejected > 0 or counts.unresolved > 0))
            or (baseline is not None and (baseline.mismatched or baseline.missing_in_legacy or baseline.only_in_legacy))
        )


class _Unroutable(StepFailure):
    """The store row exists but names no server; non-retryable until DimStore is corrected."""

    def __init__(self, store_code: str) -> None:
        from esl_service.domain.failures import DependencyKind, FailureKind

        super().__init__(STEP_DISCOVER, FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED))
        self.store_code = store_code
        self.reason_override = f"{TERMINAL_STORE_UNROUTABLE}:{store_code}"


def _provenance_payload(provenance: WarehouseProvenance) -> dict[str, object]:
    return {
        "instance": provenance.instance,
        "database": provenance.database,
        "objects": list(provenance.objects),
        "query_version": provenance.query_version,
        "isolation_level": provenance.isolation_level,
        "source_watermark": provenance.source_watermark.isoformat(),
    }


def _watermark(provenance: WarehouseProvenance) -> str:
    return provenance.source_watermark.isoformat()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "EVENT_RUN_RECOVERY_MARKED",
    "EVENT_RUN_RETRY_SCHEDULED",
    "EVENT_STEP_FAILED",
    "EVENT_STEP_RESUMED",
    "RUN_STEPS",
    "STEP_CANONICALIZE",
    "STEP_DISCOVER",
    "STEP_PERSIST",
    "STEP_READ_PEPITO_HO",
    "STEP_READ_STORE",
    "STEP_READ_WAREHOUSE",
    "TERMINAL_ACTIVE_MODE_UNSUPPORTED",
    "TERMINAL_STORE_UNROUTABLE",
    "RunOutcome",
    "RunnerExecutionPort",
    "SourcePort",
    "StepFailure",
    "WorkflowRunner",
]
