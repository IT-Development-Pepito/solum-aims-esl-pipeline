"""Integration coverage for configured schedules and auditable launch (FR-008).

The three acceptance criteria are checked against real rows: a disabled
schedule creates no run, an authorized manual launch creates an execution
carrying identity and reason, and schedule configuration, enable/disable
changes, and launch source are all audit-visible.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.domain.outcomes import ExecutionMode, TriggerType
from esl_service.domain.scheduling import (
    SCHEDULE_CREATED,
    SCHEDULE_DISABLED,
    SCHEDULE_ENABLED,
    SCHEDULER_ACTOR,
    WORKFLOW_LAUNCHED,
    InvalidManualLaunch,
    ManualLaunch,
    ScheduleDefinition,
)
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import AuditEntry, WorkflowExecution

#: The VERIFIED legacy cadence: every 30 minutes from 07:00 through 23:59.
LEGACY_CADENCE = "*/30 7-23 * * *"

WINDOW_START = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)


def jakarta(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Return the UTC instant of one Asia/Jakarta wall-clock time."""

    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Jakarta"))
    return local.astimezone(UTC)


def definition(**overrides: object) -> ScheduleDefinition:
    """Build a schedule definition, overriding only what a test needs."""

    values: dict[str, object] = {
        "workflow_name": "esl-refresh",
        "store_code": "084",
        "cron_expression": LEGACY_CADENCE,
        "timezone": "Asia/Jakarta",
        "enabled": True,
    }
    values.update(overrides)
    return ScheduleDefinition(**values)  # type: ignore[arg-type]


@pytest.fixture
def launch_repository(session: Session) -> LaunchRepository:
    """Provide the schedule and launch repository on the same transaction."""

    return LaunchRepository(session)


def scope(configuration_version_id: UUID) -> dict[str, object]:
    """Return the execution scope arguments every launch needs."""

    return {
        "mode": ExecutionMode.SHADOW,
        "correlation_id": uuid4(),
        "source_window_start": WINDOW_START,
        "source_window_end": WINDOW_END,
        "configuration_version_id": configuration_version_id,
        "rule_version": "rules-v1",
    }


def audit_actions(session: Session, resource_key: str) -> list[str]:
    """Return the audit actions recorded against one resource, in order."""

    statement = (
        select(AuditEntry.action)
        .where(AuditEntry.resource_key == resource_key)
        .order_by(AuditEntry.sequence)
    )
    return list(session.scalars(statement))


def execution_count(session: Session) -> int:
    """Return how many executions exist in this rolled-back transaction."""

    return session.scalar(select(func.count()).select_from(WorkflowExecution)) or 0


# --- a disabled schedule creates no run (acceptance criterion 1) -----------


def test_a_disabled_schedule_creates_no_execution(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """The first acceptance criterion, checked against real rows."""

    schedule = launch_repository.create_schedule(
        definition(enabled=False),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )
    before = execution_count(session)

    launched = launch_repository.launch_scheduled(
        schedule.id, instant=jakarta(2026, 8, 31, 7, 30), **scope(configuration_version_id)
    )

    assert launched.launched is False
    assert execution_count(session) == before


def test_an_enabled_schedule_outside_its_cadence_creates_no_execution(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Being enabled is not enough; the cadence still has to select the minute."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )

    launched = launch_repository.launch_scheduled(
        schedule.id, instant=jakarta(2026, 8, 31, 6, 30), **scope(configuration_version_id)
    )

    assert launched.launched is False


def test_an_enabled_schedule_launches_a_scheduled_execution(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """A due, enabled schedule creates a run whose source is SCHEDULED."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )

    launched = launch_repository.launch_scheduled(
        schedule.id, instant=jakarta(2026, 8, 31, 7, 30), **scope(configuration_version_id)
    )

    assert launched.execution is not None
    assert launched.execution.trigger_type == TriggerType.SCHEDULED.value
    assert launched.execution.workflow_name == "esl-refresh"
    assert launched.execution.store_code == "084"
    assert launched.execution.requested_by is None


def test_due_schedules_never_include_a_disabled_one(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """The due-schedule query is the same gate, so it cannot be bypassed."""

    launch_repository.create_schedule(
        definition(store_code="075", enabled=False),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )
    enabled = launch_repository.create_schedule(
        definition(store_code="084"),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )

    due = launch_repository.due_schedules(jakarta(2026, 8, 31, 7, 30))

    assert [row.id for row in due] == [enabled.id]


# --- enable and disable are controlled and audited (criterion 3) -----------


def test_disabling_then_enabling_is_audit_visible_with_before_and_after(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Every enable/disable change names who, why, and what it changed."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )

    launch_repository.set_schedule_enabled(
        schedule.id, enabled=False, actor="ops.bob", reason="INC-4242 price freeze"
    )
    launch_repository.set_schedule_enabled(
        schedule.id, enabled=True, actor="ops.bob", reason="INC-4242 resolved"
    )

    assert audit_actions(session, str(schedule.id)) == [
        SCHEDULE_CREATED,
        SCHEDULE_DISABLED,
        SCHEDULE_ENABLED,
    ]
    entries = session.scalars(
        select(AuditEntry)
        .where(AuditEntry.resource_key == str(schedule.id))
        .order_by(AuditEntry.sequence)
    ).all()
    disabling = entries[1]
    assert disabling.actor == "ops.bob"
    assert disabling.reason == "INC-4242 price freeze"
    assert disabling.before_evidence == {"enabled": True}
    assert disabling.after_evidence == {"enabled": False}


def test_disabling_a_schedule_stops_the_next_run(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """Disabling is effective, not merely recorded."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )
    due = jakarta(2026, 8, 31, 7, 30)
    assert launch_repository.launch_scheduled(
        schedule.id, instant=due, **scope(configuration_version_id)
    ).launched

    launch_repository.set_schedule_enabled(
        schedule.id, enabled=False, actor="ops.bob", reason="INC-4242 price freeze"
    )

    assert (
        launch_repository.launch_scheduled(
            schedule.id, instant=due, **scope(configuration_version_id)
        ).launched
        is False
    )


def test_creating_a_schedule_records_its_configuration_as_audit_evidence(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Schedule configuration itself is audit-visible, not just its changes."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )

    entry = session.scalars(
        select(AuditEntry).where(AuditEntry.resource_key == str(schedule.id))
    ).one()
    assert entry.after_evidence == {
        "workflow_name": "esl-refresh",
        "store_code": "084",
        "cron_expression": LEGACY_CADENCE,
        "timezone": "Asia/Jakarta",
        "enabled": True,
    }
    assert entry.configuration_version_id == configuration_version_id


def test_enabling_an_unknown_schedule_is_refused(
    launch_repository: LaunchRepository,
) -> None:
    """A change to a schedule that does not exist must not be silently ignored."""

    with pytest.raises(LookupError):
        launch_repository.set_schedule_enabled(
            uuid4(), enabled=False, actor="ops.bob", reason="INC-4242"
        )


# --- a manual launch is recorded with identity and reason (criterion 2) ----


def test_a_manual_launch_creates_an_execution_with_identity_and_reason(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """The second acceptance criterion, checked against a real row."""

    launched = launch_repository.launch_manual(
        ManualLaunch(requested_by="ops.alice", reason="INC-1234 price correction"),
        workflow_name="esl-refresh",
        store_code="084",
        **scope(configuration_version_id),
    )

    assert launched.execution is not None
    assert launched.execution.trigger_type == TriggerType.MANUAL.value
    assert launched.execution.requested_by == "ops.alice"
    assert launched.execution.reason == "INC-1234 price correction"


def test_a_manual_launch_starts_at_the_instant_it_was_launched(
    launch_repository: LaunchRepository,
    configuration_version_id: UUID,
) -> None:
    """``started_at`` is the launch instant, not a second clock read.

    The worker orders runnable executions by ``started_at``; two launches
    that read the wall clock separately can tie on a coarse host clock and
    then order by random id.
    """

    instant = datetime(2026, 8, 31, 1, 2, 3, 456789, tzinfo=UTC)

    launched = launch_repository.launch_manual(
        ManualLaunch(requested_by="ops.alice", reason="INC-1 timing"),
        workflow_name="esl-refresh",
        store_code="084",
        **scope(configuration_version_id),
        now=instant,
    )

    assert launched.execution is not None
    assert launched.execution.started_at == instant


def test_a_manual_launch_is_audit_visible_against_its_execution(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """Launch source is audit-visible, so a run's origin needs no log parsing."""

    launched = launch_repository.launch_manual(
        ManualLaunch(requested_by="ops.alice", reason="INC-1234 price correction"),
        workflow_name="esl-refresh",
        store_code="084",
        **scope(configuration_version_id),
    )

    assert launched.execution is not None
    entry = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == launched.execution.id,
            AuditEntry.action == WORKFLOW_LAUNCHED,
        )
    ).one()
    assert entry.action == WORKFLOW_LAUNCHED
    assert entry.actor == "ops.alice"
    assert entry.reason == "INC-1234 price correction"
    assert entry.after_evidence == {"trigger_type": TriggerType.MANUAL.value}


def test_a_scheduled_launch_is_audit_visible_as_scheduled(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """A timed run is distinguishable from an operator run in the audit trail."""

    schedule = launch_repository.create_schedule(
        definition(),
        configuration_version_id=configuration_version_id,
        actor="ops.alice",
        reason="CHG-9001 initial configuration",
    )
    launched = launch_repository.launch_scheduled(
        schedule.id, instant=jakarta(2026, 8, 31, 7, 30), **scope(configuration_version_id)
    )

    assert launched.execution is not None
    entry = session.scalars(
        select(AuditEntry).where(
            AuditEntry.execution_id == launched.execution.id,
            AuditEntry.action == WORKFLOW_LAUNCHED,
        )
    ).one()
    assert entry.action == WORKFLOW_LAUNCHED
    assert entry.actor == SCHEDULER_ACTOR
    assert entry.after_evidence == {
        "trigger_type": TriggerType.SCHEDULED.value,
        "schedule_id": str(schedule.id),
    }


def test_an_anonymous_manual_launch_never_reaches_the_database(
    launch_repository: LaunchRepository,
    session: Session,
    configuration_version_id: UUID,
) -> None:
    """The domain invariant holds before any row is written."""

    before = execution_count(session)

    with pytest.raises(InvalidManualLaunch):
        launch_repository.launch_manual(
            ManualLaunch(requested_by="", reason="INC-1234"),
            workflow_name="esl-refresh",
            store_code="084",
            **scope(configuration_version_id),
        )

    assert execution_count(session) == before
