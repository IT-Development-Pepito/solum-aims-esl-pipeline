"""The durable scheduler tick (FR-008, #28).

Once a minute the host asks the scheduler to tick. The scheduler launches
every due schedule through the existing ``LaunchRepository`` shape, giving
each run the reproducible window the owner chose (previous cadence instant
to this instant), a fresh correlation id, and the active configuration and
rule versions. Paused, it launches nothing and touches nothing. One
schedule's failure never silences the others: it is reported in the tick
result and the remaining schedules still launch.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import ScheduleDefinition
from esl_service.runtime.scheduler import LaunchContext, Scheduler, TickOutcome

LEGACY_CADENCE = "*/30 7-23 * * *"


def jakarta(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Jakarta")).astimezone(
        UTC
    )


@dataclass
class FakeSchedule:
    id: UUID
    workflow_name: str
    store_code: str
    cron_expression: str = LEGACY_CADENCE
    timezone: str = "Asia/Jakarta"
    enabled: bool = True


@dataclass(frozen=True)
class FakeLaunchResult:
    execution: object | None
    refusal: str | None = None

    @property
    def launched(self) -> bool:
        return self.execution is not None


@dataclass
class FakeLaunches:
    schedules: list[FakeSchedule] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    failing: set[UUID] = field(default_factory=set)
    refusing: set[UUID] = field(default_factory=set)

    def due_schedules(self, instant: datetime) -> list[FakeSchedule]:
        return [
            s
            for s in self.schedules
            if s.enabled and _definition(s).is_due(instant)
        ]

    def launch_scheduled(self, schedule_id: UUID, **fields: Any) -> FakeLaunchResult:
        self.calls.append({"schedule_id": schedule_id, **fields})
        if schedule_id in self.failing:
            raise RuntimeError("state store went away")
        if schedule_id in self.refusing:
            return FakeLaunchResult(None, "SCOPE_OWNED")
        return FakeLaunchResult(object())


def _definition(schedule: FakeSchedule) -> ScheduleDefinition:
    return ScheduleDefinition(
        workflow_name=schedule.workflow_name,
        store_code=schedule.store_code,
        cron_expression=schedule.cron_expression,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
    )


CONTEXT = LaunchContext(
    mode=ExecutionMode.SHADOW,
    configuration_version_id=uuid4(),
    rule_version="compatibility-v1",
)


@pytest.fixture
def launches() -> FakeLaunches:
    return FakeLaunches(
        schedules=[
            FakeSchedule(uuid4(), "esl-refresh", "084"),
            FakeSchedule(uuid4(), "esl-refresh", "075"),
        ]
    )


@pytest.fixture
def scheduler(launches: FakeLaunches) -> Scheduler:
    return Scheduler(launches, CONTEXT)


# --- ticking ------------------------------------------------------------------


def test_a_tick_launches_every_due_schedule_with_its_cadence_window(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    outcomes = scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    assert len(outcomes) == 2
    assert all(o.launched for o in outcomes)
    for call in launches.calls:
        assert call["instant"] == jakarta(2026, 8, 31, 7, 30)
        assert call["source_window_start"] == jakarta(2026, 8, 31, 7, 0)
        assert call["source_window_end"] == jakarta(2026, 8, 31, 7, 30)
        assert call["mode"] is ExecutionMode.SHADOW
        assert call["configuration_version_id"] == CONTEXT.configuration_version_id
        assert call["rule_version"] == "compatibility-v1"


def test_each_launch_gets_its_own_correlation_id(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    ids = {call["correlation_id"] for call in launches.calls}
    assert len(ids) == 2 and all(isinstance(i, UUID) for i in ids)


def test_a_tick_off_every_cadence_launches_nothing(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    assert scheduler.tick(jakarta(2026, 8, 31, 7, 15)) == []
    assert launches.calls == []


def test_a_refused_launch_is_an_outcome_not_an_error(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    refused = launches.schedules[0]
    launches.refusing.add(refused.id)

    outcomes = scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    by_id = {o.schedule_id: o for o in outcomes}
    assert by_id[refused.id].launched is False
    assert by_id[refused.id].error is None
    assert by_id[launches.schedules[1].id].launched is True


def test_one_failing_schedule_does_not_stop_the_others(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    failing = launches.schedules[0]
    launches.failing.add(failing.id)

    outcomes = scheduler.tick(jakarta(2026, 8, 31, 7, 30))

    by_id = {o.schedule_id: o for o in outcomes}
    assert by_id[failing.id].launched is False
    assert by_id[failing.id].error == "RuntimeError"
    assert by_id[launches.schedules[1].id].launched is True


def test_the_outcome_never_carries_the_exception_text(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    """Driver messages can embed connection strings; only the type is kept."""

    launches.failing.add(launches.schedules[0].id)

    (failed,) = [o for o in scheduler.tick(jakarta(2026, 8, 31, 7, 30)) if o.error]

    assert "state store went away" not in repr(failed)
    assert isinstance(failed, TickOutcome)


# --- pause and resume (quiesce) --------------------------------------------


def test_a_paused_scheduler_launches_nothing_and_consults_nothing(
    scheduler: Scheduler, launches: FakeLaunches
) -> None:
    scheduler.pause()

    assert scheduler.paused is True
    assert scheduler.tick(jakarta(2026, 8, 31, 7, 30)) == []
    assert launches.calls == []


def test_resume_restores_ticking(scheduler: Scheduler, launches: FakeLaunches) -> None:
    scheduler.pause()
    scheduler.resume()

    assert scheduler.paused is False
    assert len(scheduler.tick(jakarta(2026, 8, 31, 7, 30))) == 2


def test_pause_and_resume_are_idempotent(scheduler: Scheduler) -> None:
    scheduler.pause()
    scheduler.pause()
    assert scheduler.paused is True
    scheduler.resume()
    scheduler.resume()
    assert scheduler.paused is False


def test_a_naive_tick_instant_is_refused(scheduler: Scheduler) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.tick(datetime(2026, 8, 31, 7, 30))  # noqa: DTZ001
