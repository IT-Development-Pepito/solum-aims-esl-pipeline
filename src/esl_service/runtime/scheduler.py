"""The durable scheduler tick (FR-008, FR-029, #28).

Once a minute the host calls ``tick``. Every schedule that is due launches
through the existing ``LaunchRepository`` shape with the reproducible window
the owner chose (previous cadence instant to this instant, #28), a fresh
correlation id, and the active configuration and rule versions. The
repository already refuses a disabled schedule, an off-cadence instant, or
an owned scope, and audits every launch; the scheduler adds only the loop.

Two properties matter operationally. Paused, the scheduler launches nothing
and consults nothing, which is what Service Control Manager pause and stop
rely on to quiesce. And one schedule's failure never silences the others: it
becomes an outcome naming the exception *type* only, since a driver message
can embed a connection string, and the remaining schedules still launch.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import ScheduleDefinition, scheduled_source_window


@dataclass(frozen=True)
class LaunchContext:
    """What every scheduled run of this host records about its environment."""

    mode: ExecutionMode
    configuration_version_id: UUID
    rule_version: str


class ScheduleRow(Protocol):
    """The stored schedule fields the tick needs."""

    @property
    def id(self) -> UUID: ...

    @property
    def workflow_name(self) -> str: ...

    @property
    def store_code(self) -> str: ...

    @property
    def cron_expression(self) -> str: ...

    @property
    def timezone(self) -> str: ...

    @property
    def enabled(self) -> bool: ...


class LaunchResultLike(Protocol):
    @property
    def launched(self) -> bool: ...


class SchedulerPort(Protocol):
    """The ``LaunchRepository`` methods the tick uses."""

    def due_schedules(self, instant: datetime) -> Sequence[ScheduleRow]: ...

    def launch_scheduled(
        self,
        schedule_id: UUID,
        *,
        instant: datetime,
        mode: ExecutionMode,
        correlation_id: UUID,
        source_window_start: datetime,
        source_window_end: datetime,
        configuration_version_id: UUID,
        rule_version: str,
    ) -> LaunchResultLike: ...


@dataclass(frozen=True)
class TickOutcome:
    """What one due schedule did during one tick."""

    schedule_id: UUID
    launched: bool
    #: The exception type name when the launch itself failed; never its text.
    error: str | None


class Scheduler:
    """Launches due schedules once per tick unless paused."""

    def __init__(self, launches: SchedulerPort, context: LaunchContext) -> None:
        self._launches = launches
        self._context = context
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def tick(self, now: datetime) -> list[TickOutcome]:
        """Launch every due schedule at ``now``; report each outcome."""

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if self._paused:
            return []

        outcomes: list[TickOutcome] = []
        for row in self._launches.due_schedules(now):
            outcomes.append(self._launch(row, now))
        return outcomes

    def _launch(self, row: ScheduleRow, now: datetime) -> TickOutcome:
        try:
            definition = ScheduleDefinition(
                workflow_name=row.workflow_name,
                store_code=row.store_code,
                cron_expression=row.cron_expression,
                timezone=row.timezone,
                enabled=row.enabled,
            )
            start, end = scheduled_source_window(definition, now)
            result = self._launches.launch_scheduled(
                row.id,
                instant=now,
                mode=self._context.mode,
                correlation_id=uuid4(),
                source_window_start=start,
                source_window_end=end,
                configuration_version_id=self._context.configuration_version_id,
                rule_version=self._context.rule_version,
            )
        except Exception as error:  # noqa: BLE001 - one schedule must not stop the tick
            return TickOutcome(schedule_id=row.id, launched=False, error=type(error).__name__)
        return TickOutcome(schedule_id=row.id, launched=result.launched, error=None)
