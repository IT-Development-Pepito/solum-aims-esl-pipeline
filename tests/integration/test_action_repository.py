"""Integration coverage for the idempotent action ledger (FR-013).

Repeated submission or restart yields one logical action. A duplicate is an
auditable decision, not a silent discard, and an unknown outcome is surfaced
for reconciliation instead of being resubmitted.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from esl_service.domain.actions import (
    ActionAttemptEvidence,
    ActionState,
    DeliveryCertainty,
    InvalidActionTransition,
    NewRecordAction,
    build_idempotency_key,
)
from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    ProcessingStatus,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.persistence.action_repository import (
    ActionRepository,
    ConcurrentActionUpdate,
)
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.models import RecordAction
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")
WINDOW_START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 28, 7, 30, tzinfo=UTC)


@pytest.fixture
def result_id(
    session: Session,
    execution_repository: ExecutionRepository,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    configuration_version_id: UUID,
) -> UUID:
    """Persist the record outcome the action belongs to."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
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
            promotion_outcome=PromotionOutcome.SELECTED,
            current_page=1,
            desired_page=2,
            action_decision=ActionDecision.PAGE_CHANGE,
            processing_status=ProcessingStatus.ACTION_REQUIRED,
            issues=(),
        ),
    )
    session.flush()
    return result.id


def intended(result_id: UUID, execution_id: UUID | None = None, **overrides: object):
    """Build one intended page action."""

    values: dict[str, object] = {
        "execution_id": execution_id or uuid4(),
        "record_processing_result_id": result_id,
        "key": KEY,
        "label_code": "LBL-0001",
        "action_type": "PAGE_CHANGE",
        "desired_page": 2,
        "desired_state": "PAGE_2",
        "mode": ExecutionMode.ACTIVE,
        "contract_version": "aims-page-v1",
        "rule_version": "rules-v1",
        "configuration_hash": "a" * 64,
        "source_window_start": WINDOW_START,
        "source_window_end": WINDOW_END,
    }
    values.update(overrides)
    return NewRecordAction(**values)  # type: ignore[arg-type]


def _execution_of(result_id: UUID, session: Session) -> UUID:
    """Return the execution that owns the persisted record outcome."""

    from esl_service.persistence.models import RecordProcessingResult

    return session.get_one(RecordProcessingResult, result_id).execution_id


# --- one logical outcome per idempotency key --------------------------------


def test_duplicate_action_returns_the_existing_row(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """Repeated submission produces one logical action (FR-013)."""

    execution_id = _execution_of(result_id, session)
    request = intended(result_id, execution_id)

    first = action_repository.create_intended(request)
    session.flush()
    second = action_repository.create_intended(request)
    session.flush()

    assert second.id == first.id
    assert session.scalars(select(RecordAction)).all() == [first]


def test_a_restart_resolves_to_the_same_action(
    session: Session,
    action_repository: ActionRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
    result_id: UUID,
) -> None:
    """A different execution for the same logical action does not duplicate it."""

    first = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    session.flush()

    restarted = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    session.flush()
    second = action_repository.create_intended(intended(result_id, restarted.id))
    session.flush()

    assert second.id == first.id
    assert second.execution_id == first.execution_id


def test_duplicate_detection_is_audited(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """A duplicate records a decision instead of silently discarding context."""

    execution_id = _execution_of(result_id, session)
    action_repository.create_intended(intended(result_id, execution_id))
    session.flush()
    action_repository.create_intended(intended(result_id, execution_id))
    session.flush()

    events = ExecutionRepository(session).list_events(execution_id)
    assert "ACTION_DUPLICATE_DETECTED" in [item.event_type for item in events]


def test_a_different_desired_state_is_a_different_action(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """Changing the desired state yields a new logical action, not a duplicate."""

    execution_id = _execution_of(result_id, session)
    first = action_repository.create_intended(intended(result_id, execution_id))
    second = action_repository.create_intended(
        intended(result_id, execution_id, desired_page=3, desired_state="PAGE_3")
    )
    session.flush()

    assert first.id != second.id


def test_stored_idempotency_key_matches_the_domain_key(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """Persistence stores exactly the key the domain derives."""

    request = intended(result_id, _execution_of(result_id, session))
    stored = action_repository.create_intended(request)
    session.flush()

    assert stored.idempotency_key == build_idempotency_key(request)


# --- lifecycle --------------------------------------------------------------


def test_shadow_action_cannot_submit(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """A shadow run never reaches an external effect."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session), mode=ExecutionMode.SHADOW)
    )
    session.flush()

    with pytest.raises(InvalidActionTransition):
        action_repository.transition(action.id, ActionState.SUBMITTING)


def test_unknown_outcome_requires_reconciliation(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """An unknown submission is surfaced rather than resubmitted (FR-013)."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
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

    assert [item.id for item in action_repository.unresolved_actions()] == [action.id]

    with pytest.raises(InvalidActionTransition):
        action_repository.transition(action.id, ActionState.SUBMITTING)


def test_acknowledged_action_is_not_unresolved(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """A confirmed action leaves the unresolved set."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.transition(
        action.id, ActionState.ACKNOWLEDGED, acknowledgement_batch_id="batch-1"
    )
    session.flush()

    assert action_repository.unresolved_actions() == []
    stored = session.get_one(RecordAction, action.id)
    assert stored.acknowledgement_batch_id == "batch-1"
    assert stored.terminal_at is not None


def test_retryable_failure_may_submit_again(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """A retryable failure returns to SUBMITTING, unlike an unknown outcome."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.transition(action.id, ActionState.FAILED_RETRYABLE)
    action_repository.transition(action.id, ActionState.SUBMITTING)
    session.flush()

    assert session.get_one(RecordAction, action.id).state == "SUBMITTING"


def test_transition_from_a_stale_state_is_refused(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """Compare-and-set stops a second worker acting on a stale state."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    session.flush()

    with pytest.raises(ConcurrentActionUpdate):
        action_repository.transition(
            action.id, ActionState.SUBMITTING, expected_state=ActionState.INTENDED
        )


# --- attempts ---------------------------------------------------------------


def test_attempts_are_append_only_and_numbered(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """One attempt number exists once per action."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)

    def attempt(number: int) -> ActionAttemptEvidence:
        return ActionAttemptEvidence(
            attempt_number=number,
            started_at=WINDOW_START,
            ended_at=WINDOW_END,
            delivery_certainty=DeliveryCertainty.NOT_DELIVERED,
            retry_class="RETRYABLE",
            result_code="500",
            error_class="SERVER_ERROR",
            response_evidence={"detail": "server error"},
        )

    action_repository.append_attempt(action.id, attempt(1))
    action_repository.append_attempt(action.id, attempt(2))
    session.flush()

    with pytest.raises(IntegrityError):
        action_repository.append_attempt(action.id, attempt(2))
        session.flush()


def test_action_with_attempts_cannot_be_deleted(
    session: Session, action_repository: ActionRepository, result_id: UUID
) -> None:
    """Durable delivery evidence uses RESTRICT."""

    action = action_repository.create_intended(
        intended(result_id, _execution_of(result_id, session))
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    action_repository.append_attempt(
        action.id,
        ActionAttemptEvidence(
            attempt_number=1,
            started_at=WINDOW_START,
            ended_at=WINDOW_END,
            delivery_certainty=DeliveryCertainty.CONFIRMED,
            retry_class=None,
            result_code="0",
            error_class=None,
            response_evidence={"responseCode": "0"},
        ),
    )
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(delete(RecordAction).where(RecordAction.id == action.id))
        session.flush()
