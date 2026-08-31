"""Integration coverage for restart-safe execution state (FR-010).

An interrupted execution must resume or reconcile from durable state without
losing or duplicating completed work. Execution states and transitions come
from the reviewed pure-domain contract in ``esl_service.domain.workflow``
(#14); this module covers only their persistence and recovery.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from esl_service.domain.outcomes import FailureClass, TriggerType
from esl_service.domain.workflow import ExecutionStatus, InvalidWorkflowTransition
from esl_service.persistence.models import WorkflowExecution
from esl_service.persistence.repository import (
    ConcurrentExecutionUpdate,
    ExecutionRepository,
)
from tests.factories import new_execution

WINDOW_START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 28, 7, 30, tzinfo=UTC)


def _running_execution(
    repository: ExecutionRepository, configuration_version_id: UUID
) -> UUID:
    """Create an execution already advanced to RUNNING."""

    execution = repository.create_execution(new_execution(configuration_version_id))
    repository.transition_execution(
        execution.id, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
    )
    return execution.id


def test_checkpoint_survives_a_new_session(
    session: Session, connection: Connection, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """A fresh session recovers the interrupted execution, step, and checkpoint."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    step = execution_repository.start_step(execution_id, "canonicalize", attempt=1)
    execution_repository.append_checkpoint(
        step.id,
        checkpoint_key="last-record",
        checkpoint_version=1,
        watermark="084:101024011793:KGS",
        payload={"record_count": 1},
    )
    session.flush()

    with Session(bind=connection, join_transaction_mode="create_savepoint") as restarted:
        recovered = ExecutionRepository(restarted).recoverable_executions()
        assert [item.id for item in recovered] == [execution_id]
        checkpoint = recovered[0].steps[0].checkpoints[0]
        assert checkpoint.watermark.endswith(":KGS")
        assert checkpoint.payload == {"record_count": 1}
        assert recovered[0].rule_version == "rules-v1"


def test_terminal_executions_are_not_recoverable(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Completed work is never offered for recovery, so it cannot be duplicated."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    execution_repository.transition_execution(
        execution_id, ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED
    )
    session.flush()

    assert execution_repository.recoverable_executions() == []


def test_execution_delete_is_restricted_with_evidence(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Durable audit evidence blocks deleting the execution that produced it."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    execution_repository.append_event(execution_id, "TEST_EVENT", {"k": "v"})
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            delete(WorkflowExecution).where(WorkflowExecution.id == execution_id)
        )
        session.flush()


def test_only_one_execution_owns_a_scope_lease(
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """Two executions cannot own the same workflow and store scope (FR-009)."""

    first = _running_execution(execution_repository, configuration_version_id)
    second = _running_execution(execution_repository, configuration_version_id)
    now = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)

    assert execution_repository.claim_scope(first, "sku-shadow:084", now=now) is True
    assert execution_repository.claim_scope(second, "sku-shadow:084", now=now) is False


def test_heartbeat_extends_the_lease_expiry(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """A live owner extends its lease so recovery does not steal active work."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    now = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
    execution_repository.claim_scope(execution_id, "sku-shadow:084", now=now)
    session.flush()

    later = now + timedelta(minutes=5)
    assert execution_repository.heartbeat_scope("sku-shadow:084", execution_id, now=later)
    lease = execution_repository.get_lease("sku-shadow:084")
    assert lease is not None
    assert lease.heartbeat_at == later
    assert lease.expires_at > later
    assert lease.lease_version == 2


def test_expired_lease_is_discoverable_for_recovery(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """An abandoned lease becomes visible once its expiry passes."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    now = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
    execution_repository.claim_scope(execution_id, "sku-shadow:084", now=now)
    session.flush()

    assert execution_repository.expired_leases(now=now) == []
    expired = execution_repository.expired_leases(now=now + timedelta(hours=2))
    assert [lease.scope_key for lease in expired] == ["sku-shadow:084"]


def test_released_scope_can_be_claimed_again(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Releasing a lease hands the scope to the next execution without a delete."""

    first = _running_execution(execution_repository, configuration_version_id)
    second = _running_execution(execution_repository, configuration_version_id)
    now = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)

    execution_repository.claim_scope(first, "sku-shadow:084", now=now)
    session.flush()
    assert execution_repository.release_scope("sku-shadow:084", first, now=now) is True
    session.flush()

    assert execution_repository.claim_scope(second, "sku-shadow:084", now=now) is True


def test_retry_and_replay_parents_are_persisted(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Retry and replay link to their origin instead of overwriting history."""

    original = execution_repository.create_execution(new_execution(configuration_version_id))
    session.flush()
    retry = execution_repository.create_execution(
        new_execution(configuration_version_id, trigger_type=TriggerType.RETRY, retry_of_execution_id=original.id)
    )
    replay = execution_repository.create_execution(
        new_execution(
            configuration_version_id,
            trigger_type=TriggerType.REPLAY, replay_of_execution_id=original.id
        )
    )
    session.flush()

    assert retry.retry_of_execution_id == original.id
    assert replay.replay_of_execution_id == original.id
    assert retry.id != original.id and replay.id != original.id


def test_source_window_round_trips_as_utc(
    session: Session, connection: Connection, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """The reproducible source window is stored and reloaded as UTC (FR-002)."""

    execution = execution_repository.create_execution(new_execution(configuration_version_id))
    session.flush()

    with Session(bind=connection, join_transaction_mode="create_savepoint") as reloaded:
        stored = reloaded.get_one(WorkflowExecution, execution.id)
        assert stored.source_window_start.astimezone(UTC) == WINDOW_START
        assert stored.source_window_end.astimezone(UTC) == WINDOW_END


def test_accepted_transition_appends_audit_evidence(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """An accepted transition records its evidence in the same transaction."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    session.flush()

    events = execution_repository.list_events(execution_id)
    assert [event.event_type for event in events] == ["WORKFLOW_TRANSITION_ACCEPTED"]
    assert events[0].payload == {
        "from_status": "QUEUED",
        "to_status": "RUNNING",
        "reason_code": "EXECUTION_TRANSITION_ACCEPTED",
    }


def test_invalid_transition_is_rejected(
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """The persisted layer refuses a transition the domain graph forbids."""

    execution = execution_repository.create_execution(new_execution(configuration_version_id))
    with pytest.raises(InvalidWorkflowTransition):
        execution_repository.transition_execution(
            execution.id, ExecutionStatus.QUEUED, ExecutionStatus.SUCCEEDED
        )


def test_transition_from_an_unexpected_status_is_refused(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Compare-and-set stops a second worker acting on stale status."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    session.flush()

    with pytest.raises(ConcurrentExecutionUpdate):
        execution_repository.transition_execution(
            execution_id, ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
        )


def test_step_is_unique_per_execution_name_and_attempt(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """One attempt of a named step exists once, so retries are explicit."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    execution_repository.start_step(execution_id, "canonicalize", attempt=1)
    session.flush()

    with pytest.raises(IntegrityError):
        execution_repository.start_step(execution_id, "canonicalize", attempt=1)


def test_retry_attempt_of_a_step_is_recorded_separately(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """A retried step keeps the failed attempt as evidence (FR-014)."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    first = execution_repository.start_step(execution_id, "canonicalize", attempt=1)
    execution_repository.finish_step(
        first.id, outcome="FAILED", failure_class=FailureClass.RETRYABLE
    )
    second = execution_repository.start_step(execution_id, "canonicalize", attempt=2)
    session.flush()

    assert first.id != second.id
    assert first.failure_class == FailureClass.RETRYABLE.value


def test_checkpoint_is_unique_per_step_key_and_version(
    session: Session, execution_repository: ExecutionRepository,
    configuration_version_id: UUID
) -> None:
    """Checkpoints are append-only, so one key and version is written once."""

    execution_id = _running_execution(execution_repository, configuration_version_id)
    step = execution_repository.start_step(execution_id, "canonicalize", attempt=1)
    execution_repository.append_checkpoint(
        step.id,
        checkpoint_key="last-record",
        checkpoint_version=1,
        watermark="084:1:KGS",
        payload={"n": 1},
    )
    session.flush()

    with pytest.raises(IntegrityError):
        execution_repository.append_checkpoint(
            step.id,
            checkpoint_key="last-record",
            checkpoint_version=1,
            watermark="084:2:KGS",
            payload={"n": 2},
        )


def test_new_execution_requires_an_ordered_utc_window() -> None:
    """A reproducible window must be UTC-aware and correctly ordered."""

    version_id = uuid4()
    with pytest.raises(ValueError, match="timezone-aware"):
        # DTZ001 is suppressed deliberately: the naive value is the input under
        # test, and the assertion proves it is rejected rather than stored.
        new_execution(
            version_id,
            source_window_start=datetime(2026, 8, 28, 7, 0),  # noqa: DTZ001
        )
    with pytest.raises(ValueError, match="source_window_start"):
        new_execution(
            version_id, source_window_start=WINDOW_END, source_window_end=WINDOW_START
        )
