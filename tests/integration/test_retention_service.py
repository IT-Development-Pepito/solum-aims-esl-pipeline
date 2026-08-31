"""Retention eligibility guards and purge behaviour (architecture 5.8).

Purge deletes durable evidence, so each guard is tested by making exactly one
precondition fail and asserting the execution is not offered. The audit core
survives a purge and the purge records itself.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.domain.actions import (
    ActionAttemptEvidence,
    ActionState,
    DeliveryCertainty,
    NewRecordAction,
)
from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    ProcessingStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.models import (
    AuditEntry,
    ExecutionEvent,
    ReconciliationReport,
    RecordAction,
    RecordIssue,
    RecordProcessingResult,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.retention import (
    RetentionPolicy,
    RetentionRefused,
    RetentionService,
)
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
KEY = CanonicalKey("084", "101024011793", "KGS")

ENABLED = RetentionPolicy(
    purge_enabled=True,
    audit_core_days=365,
    detailed_evidence_days=90,
    compatibility_days=30,
)
DISABLED = RetentionPolicy(
    purge_enabled=False,
    audit_core_days=None,
    detailed_evidence_days=None,
    compatibility_days=None,
)


def balanced(**overrides: int) -> ReconciliationCounts:
    """Build a balanced, fully resolved ACTIVE count set."""

    values: dict[str, int] = {
        "extracted": 1,
        "rejected": 0,
        "valid": 1,
        "ineligible": 0,
        "eligible": 1,
        "unchanged": 1,
        "skipped_idempotent": 0,
        "intended": 0,
        "acknowledged": 0,
        "rejected_by_aims": 0,
        "failed": 0,
        "unresolved": 0,
        "submitted": 0,
        "ambiguous": 0,
    }
    values.update(overrides)
    return ReconciliationCounts(**values)


class Fixture:
    """One fully populated, purgeable execution and its evidence."""

    def __init__(self, execution_id: UUID, result_id: UUID) -> None:
        self.execution_id = execution_id
        self.result_id = result_id


@pytest.fixture
def populated(
    session: Session,
    execution_repository: ExecutionRepository,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    reconciliation_repository: ReconciliationRepository,
    configuration_version_id: UUID,
) -> Fixture:
    """Create a terminal, reconciled, old execution with detailed evidence."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    execution_repository.transition_execution(
        execution.id, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
    )
    step = execution_repository.start_step(execution.id, "canonicalize", attempt=1)
    execution_repository.append_checkpoint(
        step.id,
        checkpoint_key="last-record",
        checkpoint_version=1,
        watermark="084:1:KGS",
        payload={"n": 1},
    )
    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark="2026-08-28T07:00:00+00:00",
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
            promotion_outcome=PromotionOutcome.NO_PROMOTION,
            current_page=1,
            desired_page=1,
            action_decision=ActionDecision.SKIP_IDEMPOTENT,
            processing_status=ProcessingStatus.UNCHANGED,
            issues=(
                RecordIssueEvidence(
                    rule_id="BR-017",
                    issue_code="MISSING_WEEKDAY_METADATA",
                    severity="WARNING",
                    classification="PROMOTION",
                    evidence={"source_campaign_id": "A"},
                ),
            ),
        ),
    )
    execution_repository.transition_execution(
        execution.id, ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED
    )
    reconciliation_repository.finalize_report(
        execution.id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()
    _age(session, execution.id, NOW - timedelta(days=120))
    return Fixture(execution.id, result.id)


def _age(session: Session, execution_id: UUID, ended_at: datetime) -> None:
    """Backdate an execution so the age guard can be exercised."""

    from esl_service.persistence.models import WorkflowExecution

    session.get_one(WorkflowExecution, execution_id).ended_at = ended_at
    session.flush()


def service(session: Session, policy: RetentionPolicy = ENABLED) -> RetentionService:
    """Build a retention service over the test transaction."""

    return RetentionService(session, policy)


# --- eligibility guards, one failing precondition at a time -----------------


def test_a_fully_eligible_execution_is_offered(
    session: Session, populated: Fixture
) -> None:
    """The baseline fixture satisfies every guard."""

    assert service(session).find_eligible(now=NOW, limit=10) == [populated.execution_id]


def test_disabled_purge_offers_nothing(
    session: Session, populated: Fixture
) -> None:
    """With purge disabled nothing is ever eligible."""

    assert service(session, DISABLED).find_eligible(now=NOW, limit=10) == []


def test_a_recent_execution_is_not_eligible(
    session: Session, populated: Fixture
) -> None:
    """An execution inside the retention window is retained."""

    _age(session, populated.execution_id, NOW - timedelta(days=10))
    assert service(session).find_eligible(now=NOW, limit=10) == []


def test_a_non_terminal_execution_is_not_eligible(
    session: Session, execution_repository: ExecutionRepository, populated: Fixture
) -> None:
    """Only a finished execution may be purged."""

    from esl_service.persistence.models import WorkflowExecution

    session.get_one(WorkflowExecution, populated.execution_id).status = (
        ExecutionStatus.RUNNING.value
    )
    session.flush()
    assert service(session).find_eligible(now=NOW, limit=10) == []


def test_an_unfinalized_report_blocks_eligibility(
    session: Session, populated: Fixture
) -> None:
    """Evidence is retained until its reconciliation is finalized."""

    report = session.scalars(
        select(ReconciliationReport).where(
            ReconciliationReport.execution_id == populated.execution_id
        )
    ).one()
    report.status = "DRAFT"
    session.flush()
    assert service(session).find_eligible(now=NOW, limit=10) == []


def test_an_unresolved_report_blocks_eligibility(
    session: Session, populated: Fixture
) -> None:
    """A non-zero unresolved count keeps the evidence."""

    report = session.scalars(
        select(ReconciliationReport).where(
            ReconciliationReport.execution_id == populated.execution_id
        )
    ).one()
    report.unresolved = 1
    session.flush()
    assert service(session).find_eligible(now=NOW, limit=10) == []


def test_an_unknown_action_outcome_blocks_eligibility(
    session: Session,
    action_repository: ActionRepository,
    populated: Fixture,
) -> None:
    """An action whose external outcome is unknown is never purged (FR-013)."""

    action = action_repository.create_intended(
        NewRecordAction(
            execution_id=populated.execution_id,
            record_processing_result_id=populated.result_id,
            key=KEY,
            label_code="LBL-0001",
            action_type="PAGE_CHANGE",
            desired_page=2,
            desired_state="PAGE_2",
            mode=ExecutionMode.ACTIVE,
            contract_version="aims-page-v1",
            rule_version="rules-v1",
            configuration_hash="a" * 64,
            source_window_start=NOW - timedelta(days=121),
            source_window_end=NOW - timedelta(days=121),
        )
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.append_attempt(
        action.id,
        ActionAttemptEvidence(
            attempt_number=1,
            started_at=NOW - timedelta(days=121),
            ended_at=None,
            delivery_certainty=DeliveryCertainty.UNKNOWN,
            retry_class=None,
            result_code=None,
            error_class="TIMEOUT",
            response_evidence={"detail": "no response"},
        ),
    )
    action_repository.transition(action.id, ActionState.OUTCOME_UNKNOWN)
    session.flush()

    assert service(session).find_eligible(now=NOW, limit=10) == []


# --- purge behaviour --------------------------------------------------------


def test_purge_refuses_when_disabled(session: Session, populated: Fixture) -> None:
    """A disabled policy never deletes, even if asked directly."""

    with pytest.raises(RetentionRefused, match="disabled"):
        service(session, DISABLED).purge_execution(
            populated.execution_id, now=NOW, actor="operator", reason="INC-1"
        )


def test_purge_refuses_an_ineligible_execution(
    session: Session, populated: Fixture
) -> None:
    """An execution inside its retention window is refused, not deleted."""

    _age(session, populated.execution_id, NOW - timedelta(days=1))
    with pytest.raises(RetentionRefused, match="not eligible"):
        service(session).purge_execution(
            populated.execution_id, now=NOW, actor="operator", reason="INC-1"
        )
    assert _count(session, RecordIssue) == 1


def test_purge_removes_detailed_evidence(
    session: Session, populated: Fixture
) -> None:
    """Detailed processing evidence is deleted for an eligible execution."""

    outcome = service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-1"
    )
    session.flush()

    assert outcome.total > 0
    assert _count(session, RecordIssue) == 0
    assert _count(session, ExecutionEvent) == 0


def test_purge_retains_the_audit_core(
    session: Session, populated: Fixture
) -> None:
    """Executions, actions, and reconciliation summaries survive a purge."""

    service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-1"
    )
    session.flush()

    from esl_service.persistence.models import WorkflowExecution

    assert session.get(WorkflowExecution, populated.execution_id) is not None
    assert _count(session, ReconciliationReport) == 1
    assert _count(session, RecordProcessingResult) == 1


def test_purge_records_its_own_audit_entry(
    session: Session, populated: Fixture
) -> None:
    """A purge is an authorized operation and audits itself (architecture 5.8)."""

    service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-1"
    )
    session.flush()

    entry = session.scalars(
        select(AuditEntry).where(AuditEntry.action == "RETENTION_PURGE")
    ).one()
    assert entry.actor == "operator@example"
    assert entry.reason == "INC-1"
    assert entry.execution_id == populated.execution_id
    assert entry.after_evidence is not None


def test_purged_execution_is_no_longer_offered(
    session: Session, populated: Fixture
) -> None:
    """A second purge finds nothing left to delete for that execution."""

    service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-1"
    )
    session.flush()

    outcome = service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-2"
    )
    assert outcome.total == 0


def test_purge_leaves_actions_intact(
    session: Session, action_repository: ActionRepository, populated: Fixture
) -> None:
    """An acknowledged action and its key survive, since actions are audit core."""

    action = action_repository.create_intended(
        NewRecordAction(
            execution_id=populated.execution_id,
            record_processing_result_id=populated.result_id,
            key=KEY,
            label_code="LBL-0001",
            action_type="PAGE_CHANGE",
            desired_page=2,
            desired_state="PAGE_2",
            mode=ExecutionMode.ACTIVE,
            contract_version="aims-page-v1",
            rule_version="rules-v1",
            configuration_hash="a" * 64,
            source_window_start=NOW - timedelta(days=121),
            source_window_end=NOW - timedelta(days=121),
        )
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.transition(
        action.id, ActionState.ACKNOWLEDGED, acknowledgement_batch_id="batch-1"
    )
    session.flush()

    service(session).purge_execution(
        populated.execution_id, now=NOW, actor="operator@example", reason="INC-1"
    )
    session.flush()

    stored = session.get_one(RecordAction, action.id)
    assert stored.state == "ACKNOWLEDGED"
    assert stored.store_code == "084"
    assert len(stored.idempotency_key) == 64


def _count(session: Session, model: type) -> int:
    """Return how many rows of one model remain."""

    return int(session.scalars(select(func.count()).select_from(model)).one())
