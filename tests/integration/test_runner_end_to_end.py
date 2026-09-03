"""One execution from launch to a terminal state against the real state store (#102).

The sources are fakes; everything else is real: the #15 launch takes the
scope, the runner drives the #14 graph through the repository, steps and
checkpoints land in their tables, the #104 step persists the snapshot and
report, and the scope is released at the end. A retryable failure leaves the
execution in RETRY_WAIT with its evidence; a second run completes it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from esl_service.application.persist_run import persist_run
from esl_service.application.runner import (
    RUN_STEPS,
    STEP_READ_STORE,
    StepFailure,
    WorkflowRunner,
)
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    RetryPolicy,
)
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import ManualLaunch
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import (
    PromotionEvidenceRepository,
    RecordOutcomeRepository,
)
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import (
    ExecutionCheckpoint,
    ExecutionStep,
    ReconciliationReport,
    SnapshotSet,
    WorkflowExecution,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository

WINDOW = SourceWindow(datetime(2026, 9, 2, 0, 0, tzinfo=UTC), datetime(2026, 9, 2, 0, 30, tzinfo=UTC))
STORE = StoreDirectoryEntry("084", "10.0.0.84", "PEPITO_084")
POLICY = RetryPolicy(max_attempts=2, timeout_seconds=Decimal(30), initial_backoff_seconds=Decimal(1), max_backoff_seconds=Decimal(8), jitter_ratio=Decimal(0))


def provenance(instance: str, database: str, objects: tuple[str, ...]) -> WarehouseProvenance:
    return WarehouseProvenance(instance, database, objects, "test-v1", WINDOW.start, WINDOW.end, WINDOW.end)


@dataclass
class FakeSources:
    fail_store_read: FailureSignal | None = None
    calls: list[str] = field(default_factory=list)

    def discover_store(self, store_code: str, window: SourceWindow) -> StoreDirectoryEntry | None:
        self.calls.append("discover")
        return STORE

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
        rows = {name: () for name in STORE_OBJECTS}
        rows["dbo.ITEM_MST"] = (
            {"ITM_CD": "SKU-1", "ITM_STATUS": "O", "ITM_SALES_UOM": "PCS", "ITM_LONG_NAME": "Item one"},
            {"ITM_CD": "SKU-2", "ITM_STATUS": "C", "ITM_SALES_UOM": "PCS"},
        )
        rows["dbo.BASIC_SP_MST"] = ({"BSP_ITEM_CD": "SKU-1", "BSP_UOM": "PCS", "BSP_SELL_PRICE": Decimal(12500), "BSP_PRICE_CATG": "001", "BSP_STATUS": "A"},)
        return StoreReadResult.from_mapping(rows, provenance(entry.org_ip, entry.org_db, STORE_OBJECTS))

    def read_baseline(self, store_code: str, window: SourceWindow) -> BaselineReadResult | None:
        return None


@pytest.fixture
def runner_parts(session: Session) -> tuple[ExecutionRepository, WorkflowRunner, FakeSources]:
    executions = ExecutionRepository(session)
    sources = FakeSources()
    persist = partial(
        persist_run,
        executions=executions,
        snapshots=SnapshotRepository(session),
        outcomes=RecordOutcomeRepository(session),
        promotions=PromotionEvidenceRepository(session),
        actions=ActionRepository(session),
        reconciliation=ReconciliationRepository(session),
    )
    runner = WorkflowRunner(
        executions=executions,
        sources=sources,
        retry_policy=POLICY,
        persist=persist,
        clock=lambda: datetime(2026, 9, 2, 0, 31, tzinfo=UTC),
        jitter=lambda: 0.0,
        store_timezone="Asia/Jakarta",
    )
    return executions, runner, sources


def launch(
    session: Session, configuration_version_id: UUID, *, store_code: str = "084", now: datetime | None = None
) -> UUID:
    result = LaunchRepository(session).launch_manual(
        ManualLaunch(requested_by="ops.alice", reason="CHG-1"),
        workflow_name="esl-refresh",
        store_code=store_code,
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=WINDOW.start,
        source_window_end=WINDOW.end,
        configuration_version_id=configuration_version_id,
        rule_version="compatibility-v1",
        now=now,
    )
    assert result.execution is not None
    session.flush()
    return result.execution.id


def test_a_launched_execution_runs_to_succeeded_with_durable_steps_and_evidence(
    session: Session, runner_parts: tuple[ExecutionRepository, WorkflowRunner, FakeSources], configuration_version_id: UUID
) -> None:
    executions, runner, _sources = runner_parts
    execution_id = launch(session, configuration_version_id)

    outcome = runner.run(execution_id)

    session.expire_all()
    execution = session.get_one(WorkflowExecution, execution_id)
    assert outcome.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS  # SKU-2 was excluded (rejected > 0)
    assert execution.status == ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS.value
    assert execution.ended_at is not None
    steps = session.scalars(select(ExecutionStep).where(ExecutionStep.execution_id == execution_id).order_by(ExecutionStep.started_at)).all()
    assert [s.step_name for s in steps] == list(RUN_STEPS)
    assert {s.outcome for s in steps} == {"SUCCEEDED"}
    checkpoints = session.scalars(select(ExecutionCheckpoint).join(ExecutionStep).where(ExecutionStep.execution_id == execution_id)).all()
    assert {c.checkpoint_key for c in checkpoints} >= {f"{name}:done" for name in RUN_STEPS}
    snapshot_set = session.scalars(select(SnapshotSet).where(SnapshotSet.execution_id == execution_id)).one()
    assert snapshot_set.record_count == 1 and snapshot_set.aggregate_hash is not None
    report = session.scalars(select(ReconciliationReport).where(ReconciliationReport.execution_id == execution_id)).one()
    assert (report.extracted, report.rejected, report.valid, report.intended) == (2, 1, 1, 1)
    lease = executions.get_lease("esl-refresh:084")
    assert lease is not None and lease.released_at is not None


def test_a_retryable_source_failure_leaves_retry_wait_and_a_second_run_completes(
    session: Session, runner_parts: tuple[ExecutionRepository, WorkflowRunner, FakeSources], configuration_version_id: UUID
) -> None:
    executions, runner, sources = runner_parts
    execution_id = launch(session, configuration_version_id)
    sources.fail_store_read = FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)

    first = runner.run(execution_id)
    session.expire_all()
    assert first.status is ExecutionStatus.RETRY_WAIT
    assert session.get_one(WorkflowExecution, execution_id).status == "RETRY_WAIT"
    assert executions.get_lease("esl-refresh:084").released_at is None  # type: ignore[union-attr]

    sources.fail_store_read = None
    second = runner.run(execution_id)

    session.expire_all()
    assert second.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS
    attempts = session.scalars(select(ExecutionStep.attempt).where(ExecutionStep.execution_id == execution_id, ExecutionStep.step_name == STEP_READ_STORE).order_by(ExecutionStep.attempt)).all()
    assert attempts == [1, 2]
    assert sources.calls.count("discover") == 1  # resumed from the discover checkpoint


def test_runnable_executions_are_listed_oldest_first(
    session: Session, runner_parts: tuple[ExecutionRepository, WorkflowRunner, FakeSources], configuration_version_id: UUID
) -> None:
    """Oldest by launch instant, not by insertion order or by id.

    The later-inserted execution carries the earlier instant, so a listing
    that followed insertion order, or a tie broken by random id, would fail.
    """

    executions, _, _ = runner_parts
    later = launch(session, configuration_version_id, now=datetime(2026, 9, 2, 0, 0, 1, tzinfo=UTC))
    earlier = launch(session, configuration_version_id, store_code="075", now=datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC))

    ids = executions.runnable_executions(limit=10)

    assert ids == [earlier, later]


def test_step_history_returns_the_latest_attempt_per_step_with_checkpoints(
    session: Session, runner_parts: tuple[ExecutionRepository, WorkflowRunner, FakeSources], configuration_version_id: UUID
) -> None:
    executions, runner, sources = runner_parts
    execution_id = launch(session, configuration_version_id)
    sources.fail_store_read = FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)
    runner.run(execution_id)
    sources.fail_store_read = None
    runner.run(execution_id)

    history = executions.step_history(execution_id)

    by_name = {s.step_name: s for s in history}
    assert by_name[STEP_READ_STORE].attempt == 2 and by_name[STEP_READ_STORE].outcome == "SUCCEEDED"
    assert by_name["discover"].checkpoints[0].payload["store_code"] == "084"
