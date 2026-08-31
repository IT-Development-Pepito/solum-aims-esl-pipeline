"""Integration coverage for the audit ledger and reconciliation reports.

FR-021 requires a balanced report that enumerates every exception; FR-022
requires the audit to answer who, what, when, why, with which configuration
and input, to what outcome, and with what retry evidence.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
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
from esl_service.domain.reconciliation import (
    ReconciliationCounts,
    ReconciliationMode,
    UnbalancedReconciliation,
)
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.models import ReconciliationReport
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")
WINDOW_START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 28, 7, 30, tzinfo=UTC)


def balanced(**overrides: int) -> ReconciliationCounts:
    """Build a balanced ACTIVE count set."""

    values: dict[str, int] = {
        "extracted": 2,
        "rejected": 0,
        "valid": 2,
        "ineligible": 0,
        "eligible": 2,
        "unchanged": 1,
        "skipped_idempotent": 0,
        "intended": 0,
        "acknowledged": 0,
        "rejected_by_aims": 0,
        "failed": 0,
        "unresolved": 1,
        "submitted": 0,
        "ambiguous": 1,
    }
    values.update(overrides)
    return ReconciliationCounts(**values)


@pytest.fixture
def execution_id(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> UUID:
    """Create the execution being reconciled."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    session.flush()
    return execution.id


@pytest.fixture
def unresolved_result_id(
    session: Session,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
) -> UUID:
    """Persist one unresolved record with promotion anomaly evidence."""

    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution_id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark="2026-08-28T07:00:00+00:00",
        canonical_schema_version="canonical-v1",
    )
    snapshot = snapshot_repository.append_record(snapshot_set.id, canonical_record())
    result = outcome_repository.record_processing_result(
        execution_id,
        snapshot.id,
        RecordProcessingEvidence(
            key=KEY,
            validation_status=ValidationStatus.VALID,
            eligibility_status=EligibilityStatus.UNRESOLVED,
            promotion_outcome=PromotionOutcome.UNRESOLVED,
            current_page=1,
            desired_page=2,
            action_decision=ActionDecision.NONE,
            processing_status=ProcessingStatus.UNRESOLVED,
            issues=(
                RecordIssueEvidence(
                    rule_id="BR-013",
                    issue_code="UOM_RULE_REQUIRED",
                    severity="ERROR",
                    classification="PROMOTION",
                    evidence={"source_campaign_id": "A", "source_uom": "CTN"},
                ),
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
    session.flush()
    return result.id


# --- balanced reports -------------------------------------------------------


def test_finalized_report_stores_every_count(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
) -> None:
    """A finalized report records the whole balance, not a summary (FR-021)."""

    report = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    assert report.status == "FINALIZED"
    assert report.revision == 1
    assert report.mode == "ACTIVE"
    assert (report.extracted, report.eligible, report.unresolved) == (2, 2, 1)
    assert report.ambiguous == 1
    assert report.finalized_at is not None


def test_unbalanced_report_is_refused(
    reconciliation_repository: ReconciliationRepository, execution_id: UUID
) -> None:
    """An imbalance is refused rather than persisted as fact."""

    with pytest.raises(UnbalancedReconciliation):
        reconciliation_repository.finalize_report(
            execution_id, ReconciliationMode.ACTIVE, balanced(extracted=3)
        )


def test_a_new_reconciliation_creates_another_revision(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
) -> None:
    """A finalized report is immutable: re-running adds a revision (5.7)."""

    first = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()
    second = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    assert (first.revision, second.revision) == (1, 2)
    assert first.id != second.id


def test_report_revision_is_unique_per_execution(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
) -> None:
    """Two reports cannot claim the same revision of one execution."""

    reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()
    session.add(
        ReconciliationReport(
            execution_id=execution_id,
            revision=1,
            mode="ACTIVE",
            status="FINALIZED",
            extracted=0,
            rejected=0,
            valid=0,
            ineligible=0,
            eligible=0,
            unchanged=0,
            skipped_idempotent=0,
            intended=0,
            acknowledged=0,
            rejected_by_aims=0,
            failed=0,
            unresolved=0,
            submitted=0,
            ambiguous=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


# --- enumerated exceptions --------------------------------------------------


def test_exceptions_enumerate_record_issues(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
    unresolved_result_id: UUID,
) -> None:
    """Every anomaly is enumerated, not hidden in an aggregate count."""

    report = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    categories = [
        item.category for item in reconciliation_repository.list_exceptions(report.id)
    ]
    assert "UOM_RULE_REQUIRED" in categories
    assert "MISSING_WEEKDAY_METADATA" in categories


def test_exceptions_carry_their_record_reference_and_evidence(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
    unresolved_result_id: UUID,
) -> None:
    """An exception points at the record it concerns and keeps its evidence."""

    report = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    found = next(
        item
        for item in reconciliation_repository.list_exceptions(report.id)
        if item.category == "UOM_RULE_REQUIRED"
    )
    assert found.record_processing_result_id == unresolved_result_id
    assert found.store_code == "084"
    assert found.actual_evidence["source_uom"] == "CTN"
    assert found.resolution_status == "OPEN"


def test_unresolved_action_is_enumerated(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    action_repository: ActionRepository,
    execution_id: UUID,
    unresolved_result_id: UUID,
) -> None:
    """An action with an unknown outcome is an enumerated exception (FR-013)."""

    action = action_repository.create_intended(
        NewRecordAction(
            execution_id=execution_id,
            record_processing_result_id=unresolved_result_id,
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
    action_repository.append_attempt(
        action.id,
        ActionAttemptEvidence(
            attempt_number=1,
            started_at=WINDOW_START,
            ended_at=WINDOW_END,
            delivery_certainty=DeliveryCertainty.UNKNOWN,
            retry_class=None,
            result_code=None,
            error_class="TIMEOUT",
            response_evidence={"detail": "no response"},
        ),
    )
    action_repository.transition(action.id, ActionState.OUTCOME_UNKNOWN)
    session.flush()

    report = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    unresolved = next(
        item
        for item in reconciliation_repository.list_exceptions(report.id)
        if item.category == "ACTION_OUTCOME_UNKNOWN"
    )
    assert unresolved.record_action_id == action.id


def test_report_with_exceptions_cannot_be_deleted(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
    unresolved_result_id: UUID,
) -> None:
    """Durable reconciliation evidence uses RESTRICT."""

    report = reconciliation_repository.finalize_report(
        execution_id, ReconciliationMode.ACTIVE, balanced()
    )
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            delete(ReconciliationReport).where(ReconciliationReport.id == report.id)
        )
        session.flush()


# --- audit ledger (FR-022) --------------------------------------------------


def test_audit_entry_answers_who_what_when_and_why(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
    configuration_version_id: UUID,
) -> None:
    """A manual action records its actor, reason, and configuration."""

    entry = reconciliation_repository.append_audit_entry(
        actor="operator@example",
        action="RUN_REPLAY",
        reason="INC-1234",
        resource_type="workflow_execution",
        resource_key=str(execution_id),
        outcome="ACCEPTED",
        execution_id=execution_id,
        configuration_version_id=configuration_version_id,
        correlation_id=uuid4(),
        before_evidence={"schedule_enabled": True},
        after_evidence={"schedule_enabled": False},
    )
    session.flush()

    assert (entry.actor, entry.action, entry.reason) == (
        "operator@example",
        "RUN_REPLAY",
        "INC-1234",
    )
    assert entry.outcome == "ACCEPTED"
    assert entry.before_evidence == {"schedule_enabled": True}


def test_audit_entry_rejects_secret_like_evidence(
    reconciliation_repository: ReconciliationRepository, execution_id: UUID
) -> None:
    """The audit ledger may never carry a credential (NFR-009)."""

    with pytest.raises(ValueError, match="forbidden evidence key"):
        reconciliation_repository.append_audit_entry(
            actor="operator@example",
            action="ROTATE",
            reason="INC-1",
            resource_type="configuration",
            resource_key="secrets",
            outcome="ACCEPTED",
            after_evidence={"database_url": "postgresql://user:pw@host/db"},
        )


def test_audit_entry_may_exist_without_an_execution(
    session: Session, reconciliation_repository: ReconciliationRepository
) -> None:
    """Schedule and configuration actions are audited without a run (FR-008)."""

    entry = reconciliation_repository.append_audit_entry(
        actor="operator@example",
        action="DISABLE_SCHEDULE",
        reason="INC-2",
        resource_type="workflow_schedule",
        resource_key="sku-shadow:084",
        outcome="ACCEPTED",
    )
    session.flush()
    assert entry.execution_id is None


def test_query_audit_returns_an_execution_trail(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_id: UUID,
) -> None:
    """An operator can retrieve one execution's audit trail in order."""

    for action in ("RUN_START", "RUN_CANCEL"):
        reconciliation_repository.append_audit_entry(
            actor="operator@example",
            action=action,
            reason="INC-3",
            resource_type="workflow_execution",
            resource_key=str(execution_id),
            outcome="ACCEPTED",
            execution_id=execution_id,
        )
    session.flush()

    trail = reconciliation_repository.query_audit(execution_id)
    assert [item.action for item in trail] == ["RUN_START", "RUN_CANCEL"]


def test_query_events_returns_structured_execution_events(
    session: Session,
    reconciliation_repository: ReconciliationRepository,
    execution_repository: ExecutionRepository,
    execution_id: UUID,
) -> None:
    """Structured events are queryable by execution for NFR-007."""

    execution_repository.transition_execution(
        execution_id, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
    )
    session.flush()

    events = reconciliation_repository.query_events(execution_id)
    assert "WORKFLOW_TRANSITION_ACCEPTED" in [item.event_type for item in events]
