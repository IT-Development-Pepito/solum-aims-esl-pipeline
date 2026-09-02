"""The scheduler tick against real schedule and execution rows (FR-008, #28).

The unit tests prove the tick's decisions with fakes; this proves that the
real ``LaunchRepository`` satisfies the scheduler's port unchanged and that a
scheduled execution lands with the cadence-derived window, the scheduler as
its actor, and the audit trail #15 already writes.
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.domain.outcomes import ExecutionMode, TriggerType
from esl_service.domain.scheduling import WORKFLOW_LAUNCHED, ScheduleDefinition
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import AuditEntry, WorkflowExecution
from esl_service.runtime.scheduler import LaunchContext, Scheduler

LEGACY_CADENCE = "*/30 7-23 * * *"


def jakarta(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Jakarta")).astimezone(
        UTC
    )


@pytest.fixture
def scheduler(session: Session, configuration_version_id: UUID) -> Scheduler:
    return Scheduler(
        LaunchRepository(session),
        LaunchContext(
            mode=ExecutionMode.SHADOW,
            configuration_version_id=configuration_version_id,
            rule_version="compatibility-v1",
        ),
    )


def create_schedule(session: Session, configuration_version_id: UUID, store_code: str) -> UUID:
    return LaunchRepository(session).create_schedule(
        ScheduleDefinition(
            workflow_name="esl-refresh",
            store_code=store_code,
            cron_expression=LEGACY_CADENCE,
            timezone="Asia/Jakarta",
            enabled=True,
        ),
        configuration_version_id=configuration_version_id,
        actor="ops.root",
        reason="CHG-1 configuration",
    ).id


def test_a_due_tick_creates_a_scheduled_execution_with_its_cadence_window(
    scheduler: Scheduler, session: Session, configuration_version_id: UUID
) -> None:
    schedule_id = create_schedule(session, configuration_version_id, "084")

    (outcome,) = scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    assert outcome.schedule_id == schedule_id and outcome.launched
    execution = session.scalars(select(WorkflowExecution)).one()
    assert execution.trigger_type == TriggerType.SCHEDULED.value
    assert execution.source_window_start == jakarta(2026, 8, 31, 7, 0)
    assert execution.source_window_end == jakarta(2026, 8, 31, 7, 30)
    assert execution.requested_by is None
    assert execution.configuration_version_id == configuration_version_id
    launched = session.scalars(
        select(AuditEntry).where(AuditEntry.action == WORKFLOW_LAUNCHED)
    ).one()
    assert launched.execution_id == execution.id


def test_a_tick_off_the_cadence_creates_nothing(
    scheduler: Scheduler, session: Session, configuration_version_id: UUID
) -> None:
    create_schedule(session, configuration_version_id, "084")

    assert scheduler.tick(jakarta(2026, 8, 31, 7, 15)) == []
    assert session.scalars(select(WorkflowExecution)).all() == []


def test_a_second_tick_at_the_same_instant_is_refused_by_ownership_not_duplicated(
    scheduler: Scheduler, session: Session, configuration_version_id: UUID
) -> None:
    """The first run still owns the scope, so the tick reports a refusal."""

    create_schedule(session, configuration_version_id, "084")
    scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    (second,) = scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    assert second.launched is False and second.error is None
    assert len(session.scalars(select(WorkflowExecution)).all()) == 1


def test_a_paused_scheduler_creates_nothing_even_when_due(
    scheduler: Scheduler, session: Session, configuration_version_id: UUID
) -> None:
    create_schedule(session, configuration_version_id, "084")
    scheduler.pause()

    assert scheduler.tick(jakarta(2026, 8, 31, 7, 30)) == []
    assert session.scalars(select(WorkflowExecution)).all() == []
