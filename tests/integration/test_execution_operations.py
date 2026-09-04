"""PostgreSQL status, retry, and bounded replay controls (FR-011, FR-012)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.domain.actions import ActionState, NewRecordAction
from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.operations import (
    WORKFLOW_RETRY_REFUSED,
    ExecutionQuery,
    ReplayRequest,
    RetryRefusalReason,
    RetryRequest,
)
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    ProcessingStatus,
    RecordProcessingEvidence,
    TriggerType,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.domain.scheduling import WORKFLOW_LAUNCHED
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import AuditEntry, WorkflowExecution
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")
WINDOW_START = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)
REPLAY_START = datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
REPLAY_END = datetime(2026, 8, 30, 23, 0, tzinfo=UTC)


def _failed_execution(
    repository: ExecutionRepository,
    configuration_version_id: UUID,
    **overrides: object,
) -> WorkflowExecution:
    execution = repository.create_execution(
        new_execution(configuration_version_id, **overrides)
    )
    repository.transition_execution(
        execution.id, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
    )
    return repository.transition_execution(
        execution.id,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
        terminal_reason="SQL_SOURCE_TIMEOUT",
    )


def _add_unresolved_action(
    *,
    execution: WorkflowExecution,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    action_repository: ActionRepository,
) -> None:
    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark=WINDOW_START.isoformat(),
        canonical_schema_version="canonical-v1",
    )
    snapshot = snapshot_repository.append_record(snapshot_set.id, canonical_record())
    result = outcome_repository.record_processing_result(
        execution.id,
        snapshot.id,
        RecordProcessingEvidence(
            key=KEY,
            validation_status=ValidationStatus.VALID,
            eligibility_status=EligibilityStatus.ELIGIBLE,
            promotion_outcome=PromotionOutcome.SELECTED,
            current_page=1,
            desired_page=2,
            action_decision=ActionDecision.PAGE_CHANGE,
            processing_status=ProcessingStatus.ACTION_REQUIRED,
            issues=(),
        ),
    )
    action = action_repository.create_intended(
        NewRecordAction(
            execution_id=execution.id,
            record_processing_result_id=result.id,
            key=KEY,
            label_code="LBL-0001",
            action_type="PAGE_CHANGE",
            desired_page=2,
            desired_state="PAGE_2",
            mode=ExecutionMode.ACTIVE,
            contract_version="aims-page-v1",
            rule_version="rules-v1",
            configuration_hash="a" * 64,
            source_window_start=WINDOW_START,
            source_window_end=WINDOW_END,
        )
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.transition(action.id, ActionState.OUTCOME_UNKNOWN)


def test_status_query_returns_state_and_terminal_reason_by_execution_id(
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-012: current state and reason are columns, not parsed log text."""

    failed = _failed_execution(execution_repository, configuration_version_id)

    found = execution_repository.query_executions(
        ExecutionQuery(execution_id=failed.id)
    )

    assert [row.id for row in found] == [failed.id]
    assert found[0].status == ExecutionStatus.FAILED.value
    assert found[0].terminal_reason == "SQL_SOURCE_TIMEOUT"


def test_status_query_filters_by_workflow_store_and_started_range(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-012: every approved selector is composable and deterministic."""

    matching = execution_repository.create_execution(
        new_execution(
            configuration_version_id,
            workflow_name="esl-refresh",
            store_code="084",
        )
    )
    matching.started_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    wrong_store = execution_repository.create_execution(
        new_execution(
            configuration_version_id,
            workflow_name="esl-refresh",
            store_code="075",
        )
    )
    wrong_store.started_at = datetime(2026, 8, 31, 10, 5, tzinfo=UTC)
    outside_range = execution_repository.create_execution(
        new_execution(
            configuration_version_id,
            workflow_name="esl-refresh",
            store_code="084",
        )
    )
    outside_range.started_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    session.flush()

    found = execution_repository.query_executions(
        ExecutionQuery(
            workflow_name="esl-refresh",
            store_code="084",
            started_from=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            started_to=datetime(2026, 8, 31, 11, 0, tzinfo=UTC),
        )
    )

    assert [row.id for row in found] == [matching.id]


def test_failed_retry_creates_a_linked_audited_execution(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-011: retry preserves reproducible scope and immutable history."""

    original = _failed_execution(execution_repository, configuration_version_id)
    launched = LaunchRepository(session).launch_retry(
        original.id,
        RetryRequest(requested_by="ops.alice", reason="INC-1234 source restored"),
        correlation_id=uuid4(),
    )

    assert launched.execution is not None
    retry = launched.execution
    assert retry.id != original.id
    assert retry.trigger_type == TriggerType.RETRY.value
    assert retry.retry_of_execution_id == original.id
    assert retry.replay_of_execution_id is None
    assert retry.workflow_name == original.workflow_name
    assert retry.store_code == original.store_code
    assert retry.mode == original.mode
    assert retry.source_window_start == original.source_window_start
    assert retry.source_window_end == original.source_window_end
    assert retry.configuration_version_id == original.configuration_version_id
    assert retry.rule_version == original.rule_version
    assert retry.requested_by == "ops.alice"
    assert retry.reason == "INC-1234 source restored"

    audit = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == retry.id,
            AuditEntry.action == WORKFLOW_LAUNCHED,
        )
    ).one()
    assert audit.after_evidence == {
        "trigger_type": TriggerType.RETRY.value,
        "retry_of_execution_id": str(original.id),
    }


def test_retry_of_a_non_failed_execution_is_refused_and_audited(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-011: retry never broadens FAILED to another execution state."""

    original = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    result = LaunchRepository(session).launch_retry(
        original.id,
        RetryRequest(requested_by="ops.alice", reason="INC-1234"),
        correlation_id=uuid4(),
    )

    assert result.execution is None
    assert result.control_refusal is RetryRefusalReason.EXECUTION_NOT_FAILED
    audit = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == original.id,
            AuditEntry.action == WORKFLOW_RETRY_REFUSED,
        )
    ).one()
    assert audit.actor == "ops.alice"
    assert audit.reason == "INC-1234"
    assert audit.outcome == RetryRefusalReason.EXECUTION_NOT_FAILED.value


def test_retry_with_an_unresolved_action_is_refused_and_audited(
    session: Session,
    execution_repository: ExecutionRepository,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    action_repository: ActionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-011/FR-013: an ambiguous AIMS effect blocks blind run retry."""

    original = _failed_execution(
        execution_repository,
        configuration_version_id,
        mode=ExecutionMode.ACTIVE,
    )
    _add_unresolved_action(
        execution=original,
        snapshot_repository=snapshot_repository,
        outcome_repository=outcome_repository,
        action_repository=action_repository,
    )

    result = LaunchRepository(session).launch_retry(
        original.id,
        RetryRequest(requested_by="ops.alice", reason="INC-1234"),
        correlation_id=uuid4(),
    )

    assert result.execution is None
    assert result.control_refusal is RetryRefusalReason.UNRESOLVED_EXTERNAL_ACTION
    audit = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == original.id,
            AuditEntry.action == WORKFLOW_RETRY_REFUSED,
        )
    ).one()
    assert audit.outcome == RetryRefusalReason.UNRESOLVED_EXTERNAL_ACTION.value


def test_bounded_replay_creates_a_linked_audited_execution(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-011: replay replaces only the explicitly approved source window."""

    original = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    launched = LaunchRepository(session).launch_replay(
        original.id,
        ReplayRequest(
            requested_by="ops.alice",
            reason="INC-5678 corrected source",
            source_window_start=REPLAY_START,
            source_window_end=REPLAY_END,
        ),
        correlation_id=uuid4(),
    )

    assert launched.execution is not None
    replay = launched.execution
    assert replay.id != original.id
    assert replay.trigger_type == TriggerType.REPLAY.value
    assert replay.retry_of_execution_id is None
    assert replay.replay_of_execution_id == original.id
    assert replay.source_window_start == REPLAY_START
    assert replay.source_window_end == REPLAY_END
    assert replay.workflow_name == original.workflow_name
    assert replay.store_code == original.store_code
    assert replay.mode == original.mode
    assert replay.configuration_version_id == original.configuration_version_id
    assert replay.rule_version == original.rule_version

    audit = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == replay.id,
            AuditEntry.action == WORKFLOW_LAUNCHED,
        )
    ).one()
    assert audit.after_evidence == {
        "trigger_type": TriggerType.REPLAY.value,
        "replay_of_execution_id": str(original.id),
        "source_window_start": REPLAY_START.isoformat(),
        "source_window_end": REPLAY_END.isoformat(),
    }


# --- snapshot replay (#114) -----------------------------------------------------


def _reconciled_original(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> WorkflowExecution:
    """An original run with a finalized SOURCE_EXPECTED capture and a finalized report."""

    from esl_service.domain.reconciliation import (
        ReconciliationCounts,
        ReconciliationMode,
    )
    from esl_service.persistence.reconciliation_repository import (
        ReconciliationRepository,
    )

    original = execution_repository.create_execution(new_execution(configuration_version_id))
    snapshots = SnapshotRepository(session)
    capture = snapshots.create_snapshot_set(
        execution_id=original.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="test",
        source_watermark="w",
        canonical_schema_version="canonical-v1",
    )
    snapshots.append_record(capture.id, canonical_record())
    snapshots.finalize_snapshot_set(capture.id)
    ReconciliationRepository(session).finalize_report(
        original.id,
        ReconciliationMode.SHADOW,
        ReconciliationCounts(1, 0, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    )
    session.flush()
    return original


def test_a_snapshot_replay_is_linked_and_carries_the_original_window_and_versions(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> None:
    from esl_service.domain.operations import SnapshotReplayRequest

    original = _reconciled_original(session, execution_repository, configuration_version_id)

    launched = LaunchRepository(session).launch_snapshot_replay(
        original.id,
        SnapshotReplayRequest(requested_by="ops.alice", reason="INC-9 reproduce"),
        correlation_id=uuid4(),
    )

    assert launched.execution is not None
    replay = launched.execution
    assert replay.trigger_type == TriggerType.SNAPSHOT_REPLAY.value
    assert replay.replay_of_execution_id == original.id
    assert (replay.source_window_start, replay.source_window_end) == (
        original.source_window_start, original.source_window_end,
    )
    assert replay.configuration_version_id == original.configuration_version_id
    assert replay.rule_version == original.rule_version
    audit = session.scalars(
        select(AuditEntry).where(AuditEntry.execution_id == replay.id, AuditEntry.action == WORKFLOW_LAUNCHED)
    ).one()
    assert audit.after_evidence["trigger_type"] == TriggerType.SNAPSHOT_REPLAY.value
    assert audit.after_evidence["replay_of_execution_id"] == str(original.id)


def test_a_snapshot_replay_of_purged_evidence_is_refused_and_audited(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> None:
    from esl_service.domain.operations import (
        WORKFLOW_SNAPSHOT_REPLAY_REFUSED,
        SnapshotReplayRefusalReason,
        SnapshotReplayRequest,
    )

    original = execution_repository.create_execution(new_execution(configuration_version_id))

    launched = LaunchRepository(session).launch_snapshot_replay(
        original.id,
        SnapshotReplayRequest(requested_by="ops.alice", reason="INC-9"),
        correlation_id=uuid4(),
    )

    assert launched.execution is None
    assert launched.control_refusal is SnapshotReplayRefusalReason.SNAPSHOT_EVIDENCE_MISSING
    refusal = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == original.id, AuditEntry.action == WORKFLOW_SNAPSHOT_REPLAY_REFUSED
        )
    ).one()
    assert refusal.outcome == "SNAPSHOT_EVIDENCE_MISSING"


def test_a_snapshot_replay_of_an_unreconciled_run_is_refused(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> None:
    from esl_service.domain.operations import (
        SnapshotReplayRefusalReason,
        SnapshotReplayRequest,
    )

    original = execution_repository.create_execution(new_execution(configuration_version_id))
    snapshots = SnapshotRepository(session)
    capture = snapshots.create_snapshot_set(
        execution_id=original.id, representation_kind="SOURCE_EXPECTED", adapter_name="test",
        source_watermark="w", canonical_schema_version="canonical-v1",
    )
    snapshots.append_record(capture.id, canonical_record())
    snapshots.finalize_snapshot_set(capture.id)

    launched = LaunchRepository(session).launch_snapshot_replay(
        original.id,
        SnapshotReplayRequest(requested_by="ops.alice", reason="INC-9"),
        correlation_id=uuid4(),
    )

    assert launched.execution is None
    assert launched.control_refusal is SnapshotReplayRefusalReason.RECONCILIATION_UNRESOLVED
