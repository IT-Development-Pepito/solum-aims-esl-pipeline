"""Integration coverage for per-scope ownership at launch (FR-009, FR-017).

Contention is exercised the way the existing lease tests exercise it: two
attempts against the same scope inside one transaction, which is what the
atomic claim predicate actually serializes on. The three acceptance criteria
are checked against real rows -- exactly one owner, an explicit rejection for
the loser, and the decision, owner, and outcome all audit-visible.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.domain.outcomes import ExecutionMode, TriggerType
from esl_service.domain.ownership import (
    OWNERSHIP_POLICY_VERSION,
    SCOPE_GRANTED,
    SCOPE_REJECTED,
    OwnershipOutcome,
    scope_key,
)
from esl_service.domain.scheduling import ManualLaunch, ScheduleDefinition
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import AuditEntry, ScopeLease, WorkflowExecution
from esl_service.persistence.repository import ExecutionRepository

NOW = datetime(2026, 8, 31, 7, 30, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)
SCOPE = scope_key("esl-refresh", "084")

#: The VERIFIED legacy cadence. NOW is 14:30 in Asia/Jakarta, so it matches.
LEGACY_CADENCE = "*/30 7-23 * * *"


@pytest.fixture
def launch_repository(session: Session) -> LaunchRepository:
    """Provide the schedule and launch repository on the same transaction."""

    return LaunchRepository(session)


def a_schedule(
    repository: LaunchRepository, configuration_version_id: UUID
) -> object:
    """Create one enabled schedule whose cadence selects NOW."""

    return repository.create_schedule(
        ScheduleDefinition(
            workflow_name="esl-refresh",
            store_code="084",
            cron_expression=LEGACY_CADENCE,
            timezone="Asia/Jakarta",
            enabled=True,
        ),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )


def scope_args(configuration_version_id: UUID) -> dict[str, object]:
    """Return the execution scope arguments every launch needs."""

    return {
        "mode": ExecutionMode.SHADOW,
        "correlation_id": uuid4(),
        "source_window_start": WINDOW_START,
        "source_window_end": WINDOW_END,
        "configuration_version_id": configuration_version_id,
        "rule_version": "rules-v1",
    }


def manual(
    repository: LaunchRepository,
    configuration_version_id: UUID,
    *,
    store_code: str = "084",
    requested_by: str = "ops.alice",
    now: datetime = NOW,
) -> object:
    """Attempt one manual launch against a workflow and store."""

    return repository.launch_manual(
        ManualLaunch(requested_by=requested_by, reason="INC-1234 price correction"),
        workflow_name="esl-refresh",
        store_code=store_code,
        now=now,
        **scope_args(configuration_version_id),
    )


def execution_count(session: Session) -> int:
    """Return how many executions exist in this rolled-back transaction."""

    return session.scalar(select(func.count()).select_from(WorkflowExecution)) or 0


# --- exactly one owner, one explicit refusal (acceptance criterion 1) ------


def test_a_second_launch_on_a_held_scope_is_rejected(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """One owner and one explicit rejected outcome, checked against real rows."""

    first = manual(launch_repository, configuration_version_id)
    after_first = execution_count(session)

    second = manual(launch_repository, configuration_version_id, requested_by="ops.bob")

    assert first.launched is True
    assert second.launched is False
    assert second.ownership is not None
    assert second.ownership.outcome is OwnershipOutcome.REJECTED
    assert execution_count(session) == after_first


def test_the_rejected_attempt_creates_no_execution_row(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """A refused launch leaves nothing queued that nothing will ever start."""

    manual(launch_repository, configuration_version_id)
    before = execution_count(session)

    manual(launch_repository, configuration_version_id, requested_by="ops.bob")

    assert execution_count(session) == before


def test_exactly_one_lease_exists_for_the_contended_scope(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """The durable guarantee is one current owner per scope."""

    first = manual(launch_repository, configuration_version_id)
    manual(launch_repository, configuration_version_id, requested_by="ops.bob")

    leases = session.scalars(
        select(ScopeLease).where(ScopeLease.scope_key == SCOPE)
    ).all()
    assert len(leases) == 1
    assert leases[0].execution_id == first.execution.id


def test_a_different_store_is_a_different_scope(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """Ownership is per workflow and store, so two stores never contend."""

    first = manual(launch_repository, configuration_version_id, store_code="084")
    second = manual(launch_repository, configuration_version_id, store_code="075")

    assert first.launched is True
    assert second.launched is True


def test_a_released_scope_can_be_taken_by_the_next_launch(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Ownership is exclusive while held, not permanent."""

    first = manual(launch_repository, configuration_version_id)
    ExecutionRepository(session).release_scope(SCOPE, first.execution.id)

    second = manual(launch_repository, configuration_version_id, requested_by="ops.bob")

    assert second.launched is True


def test_an_expired_lease_does_not_block_a_later_launch(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """An owner that stopped heartbeating no longer holds the scope."""

    manual(launch_repository, configuration_version_id)

    later = manual(
        launch_repository,
        configuration_version_id,
        requested_by="ops.bob",
        now=NOW + timedelta(hours=1),
    )

    assert later.launched is True


# --- neither trigger type preempts the other (acceptance criterion 2) -----


def test_a_manual_launch_does_not_displace_a_scheduled_owner(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """FR-017 under the initial no-simultaneous-ownership policy."""

    schedule = a_schedule(launch_repository, configuration_version_id)
    scheduled = launch_repository.launch_scheduled(
        schedule.id, instant=NOW, now=NOW, **scope_args(configuration_version_id)
    )
    assert scheduled.launched is True

    attempted = manual(launch_repository, configuration_version_id)

    assert attempted.launched is False
    assert attempted.ownership is not None
    assert attempted.ownership.owner_trigger_type is TriggerType.SCHEDULED
    assert (
        attempted.ownership.current_owner_execution_id == scheduled.execution.id
    )


def test_a_scheduled_launch_does_not_displace_a_manual_owner(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """The policy is symmetric, so it asserts no priority in either direction."""

    held = manual(launch_repository, configuration_version_id)
    schedule = a_schedule(launch_repository, configuration_version_id)

    attempted = launch_repository.launch_scheduled(
        schedule.id, instant=NOW, now=NOW, **scope_args(configuration_version_id)
    )

    assert attempted.launched is False
    assert attempted.ownership is not None
    assert attempted.ownership.owner_trigger_type is TriggerType.MANUAL
    assert attempted.ownership.current_owner_execution_id == held.execution.id


# --- the decision is audit-visible (acceptance criterion 3) ---------------


def test_a_rejected_launch_is_audit_visible_with_owner_and_policy(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """The audit answers who held the scope, what was refused, and under which policy."""

    first = manual(launch_repository, configuration_version_id)
    manual(launch_repository, configuration_version_id, requested_by="ops.bob")

    entry = session.scalars(
        select(AuditEntry)
        .where(AuditEntry.action == SCOPE_REJECTED)
        .order_by(AuditEntry.sequence)
    ).one()
    assert entry.actor == "ops.bob"
    assert entry.resource_key == SCOPE
    assert entry.outcome == OwnershipOutcome.REJECTED.value
    assert entry.execution_id is None
    assert entry.after_evidence == {
        "policy_version": OWNERSHIP_POLICY_VERSION,
        "scope_key": SCOPE,
        "outcome": OwnershipOutcome.REJECTED.value,
        "requested_trigger_type": TriggerType.MANUAL.value,
        "owner_trigger_type": TriggerType.MANUAL.value,
        "current_owner_execution_id": str(first.execution.id),
    }


def test_a_granted_launch_is_audit_visible_as_granted(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Ownership is recorded when taken, not only when refused."""

    result = manual(launch_repository, configuration_version_id)

    entry = session.scalars(
        select(AuditEntry).where(AuditEntry.action == SCOPE_GRANTED)
    ).one()
    assert entry.resource_key == SCOPE
    assert entry.outcome == OwnershipOutcome.GRANTED.value
    assert entry.execution_id == result.execution.id
    assert entry.after_evidence is not None
    assert entry.after_evidence["policy_version"] == OWNERSHIP_POLICY_VERSION
