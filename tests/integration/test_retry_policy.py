"""Failure injection against the durable attempt ledger (FR-014, FR-015).

Proves that a retryable dependency failure is retried up to the configured
limit and no further, that every attempt is recorded, and that a
non-retryable or operator-action-required failure is never retried at all.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from esl_service.domain.actions import (
    ActionAttemptEvidence,
    ActionState,
    DeliveryCertainty,
    NewRecordAction,
)
from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    RetryPolicy,
    classify,
)
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    FailureClass,
    ProcessingStatus,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.models import ActionAttempt, RecordAction
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")
START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)

POLICY = RetryPolicy(
    max_attempts=3,
    timeout_seconds=Decimal(30),
    initial_backoff_seconds=Decimal(1),
    max_backoff_seconds=Decimal(60),
    jitter_ratio=Decimal("0.5"),
)


@pytest.fixture
def action_id(
    session: Session,
    execution_repository: ExecutionRepository,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    action_repository: ActionRepository,
    configuration_version_id: UUID,
) -> UUID:
    """Create one submitted action whose attempts can be injected."""

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
            source_window_start=START,
            source_window_end=START,
        )
    )
    action_repository.transition(action.id, ActionState.SUBMITTING)
    session.flush()
    return action.id


def _inject(
    repository: ActionRepository,
    action_id: UUID,
    attempt: int,
    certainty: DeliveryCertainty,
    error_class: str,
) -> None:
    """Record one failed delivery attempt."""

    repository.append_attempt(
        action_id,
        ActionAttemptEvidence(
            attempt_number=attempt,
            started_at=START + timedelta(seconds=attempt),
            ended_at=START + timedelta(seconds=attempt, milliseconds=500),
            delivery_certainty=certainty,
            retry_class=None,
            result_code=None,
            error_class=error_class,
            response_evidence={"detail": "injected failure"},
        ),
    )


def _attempts(session: Session, action_id: UUID) -> list[ActionAttempt]:
    """Return one action's recorded attempts in order."""

    return list(session.get_one(RecordAction, action_id).attempts)


def test_retryable_failure_stops_at_the_configured_limit(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """A retryable dependency failure is retried no further than configured."""

    signal = FailureSignal(DependencyKind.AIMS_API, FailureKind.UNAVAILABLE)
    failure_class = classify(signal)
    attempt = 1

    while True:
        _inject(
            action_repository,
            action_id,
            attempt,
            DeliveryCertainty.NOT_DELIVERED,
            "UNAVAILABLE",
        )
        if not POLICY.should_retry(failure_class, attempt):
            break
        attempt += 1
    session.flush()

    assert attempt == POLICY.max_attempts
    recorded = _attempts(session, action_id)
    assert [item.attempt_number for item in recorded] == [1, 2, 3]


def test_every_attempt_is_recorded_with_its_evidence(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """No attempt is lost, so an operator can see the whole history."""

    for attempt in (1, 2, 3):
        _inject(
            action_repository,
            action_id,
            attempt,
            DeliveryCertainty.NOT_DELIVERED,
            "UNAVAILABLE",
        )
    session.flush()

    recorded = _attempts(session, action_id)
    assert len(recorded) == 3
    for item in recorded:
        assert item.delivery_certainty == "NOT_DELIVERED"
        assert item.error_class == "UNAVAILABLE"
        assert item.response_evidence == {"detail": "injected failure"}


def test_a_non_retryable_failure_is_attempted_once(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """An AIMS rejection is not retried; it is corrected first."""

    failure_class = classify(
        FailureSignal(DependencyKind.AIMS_API, FailureKind.REJECTION)
    )
    _inject(
        action_repository,
        action_id,
        1,
        DeliveryCertainty.CONFIRMED,
        "REJECTED",
    )
    session.flush()

    assert failure_class is FailureClass.NON_RETRYABLE
    assert POLICY.should_retry(failure_class, attempt=1) is False
    assert len(_attempts(session, action_id)) == 1


def test_an_unknown_outcome_is_never_retried(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """An unverified submission needs an operator, not another attempt."""

    failure_class = classify(
        FailureSignal(DependencyKind.AIMS_API, FailureKind.OUTCOME_UNKNOWN)
    )
    _inject(action_repository, action_id, 1, DeliveryCertainty.UNKNOWN, "TIMEOUT")
    action_repository.transition(action_id, ActionState.OUTCOME_UNKNOWN)
    session.flush()

    assert failure_class is FailureClass.OPERATOR_ACTION_REQUIRED
    assert POLICY.should_retry(failure_class, attempt=1) is False
    assert action_repository.unresolved_actions()[0].id == action_id


def test_exhausted_retries_reach_a_terminal_failure(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """When the limit is reached the action records a terminal failure."""

    for attempt in (1, 2, 3):
        _inject(
            action_repository,
            action_id,
            attempt,
            DeliveryCertainty.NOT_DELIVERED,
            "UNAVAILABLE",
        )
    action_repository.transition(action_id, ActionState.FAILED_TERMINAL)
    session.flush()

    stored = session.get_one(RecordAction, action_id)
    assert stored.state == "FAILED_TERMINAL"
    assert stored.terminal_at is not None
    assert len(_attempts(session, action_id)) == 3


def test_a_retryable_failure_may_return_to_submitting(
    session: Session, action_repository: ActionRepository, action_id: UUID
) -> None:
    """Within the limit, a retryable failure resubmits under the same key."""

    _inject(
        action_repository,
        action_id,
        1,
        DeliveryCertainty.NOT_DELIVERED,
        "UNAVAILABLE",
    )
    action_repository.transition(action_id, ActionState.FAILED_RETRYABLE)
    action_repository.transition(action_id, ActionState.SUBMITTING)
    session.flush()

    stored = session.get_one(RecordAction, action_id)
    assert stored.state == "SUBMITTING"
    assert len(stored.idempotency_key) == 64
