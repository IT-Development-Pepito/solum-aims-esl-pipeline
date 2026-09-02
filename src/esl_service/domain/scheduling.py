"""Recurring schedules and auditable launch decisions (FR-008).

A schedule answers exactly one question: is a run due for this workflow and
store at this instant. It does not decide scope ownership, which is FR-009 and
FR-017 and belongs to the lease, and it does not retry or replay, which is
FR-011 and belongs to #16.

Two rules are enforced here because both are acceptance criteria. A disabled
schedule launches nothing, and it is refused for being disabled rather than for
its timing, so the audit reason is never misleading. A manual launch carries an
operator identity and a reason, so no run can enter the trail anonymously.

Whether the named operator is *permitted* to launch is FR-023 role checking and
belongs to #26. This module establishes that an identity and a reason exist and
are recorded; it does not evaluate them.

Cron support is a deliberately narrow subset of the standard five-field form.
An expression this module cannot read is rejected rather than partially
understood, because a silently mis-parsed cadence would run the wrong workflow
at the wrong time and look configured while doing it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from esl_service.domain.outcomes import ExecutionMode, NewExecution, TriggerType

#: Audit action names. Both repositories and readers use this one vocabulary so
#: a schedule's history can be queried without guessing at spelling (FR-008).
SCHEDULE_CREATED = "schedule.created"
SCHEDULE_ENABLED = "schedule.enabled"
SCHEDULE_DISABLED = "schedule.disabled"
WORKFLOW_LAUNCHED = "workflow.launched"

#: Audit actor recorded for a timed run. A schedule has no operator, so the
#: trail names the scheduler and carries the schedule id as evidence rather
#: than borrowing an identity that did not act.
SCHEDULER_ACTOR = "scheduler"

#: Audit resource types for the two entities this module concerns.
SCHEDULE_RESOURCE = "workflow_schedule"
EXECUTION_RESOURCE = "workflow_execution"


class InvalidCronExpression(ValueError):
    """Raised when a cadence cannot be read as a supported cron expression."""


class InvalidManualLaunch(ValueError):
    """Raised when an operator-initiated launch lacks identity or reason."""


class LaunchRefusalReason(StrEnum):
    """Why a scheduled launch did not happen."""

    SCHEDULE_DISABLED = "SCHEDULE_DISABLED"
    NOT_DUE = "NOT_DUE"


#: Inclusive bounds of each cron field, in field order.
_FIELD_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)


def _parse_field(raw: str, name: str, low: int, high: int) -> frozenset[int]:
    """Return every value one cron field selects.

    Supports ``*``, a single value, ``A-B`` ranges, ``/S`` steps on either,
    and comma-separated lists of those. Anything else, including month and
    weekday names and ``@`` shorthands, is refused.
    """

    selected: set[int] = set()
    for part in raw.split(","):
        step = 1
        body = part
        if "/" in part:
            body, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise InvalidCronExpression(f"{name} has an invalid step: {part!r}")
            step = int(raw_step)

        if body == "*":
            start, end = low, high
        elif "-" in body.lstrip("-"):
            raw_start, _, raw_end = body.partition("-")
            if not (raw_start.isdigit() and raw_end.isdigit()):
                raise InvalidCronExpression(f"{name} has an invalid range: {part!r}")
            start, end = int(raw_start), int(raw_end)
            if start > end:
                raise InvalidCronExpression(f"{name} range is inverted: {part!r}")
        elif body.isdigit():
            start = end = int(body)
        else:
            raise InvalidCronExpression(f"{name} is not supported: {part!r}")

        if start < low or end > high:
            raise InvalidCronExpression(
                f"{name} must be within {low}-{high}: {part!r}"
            )
        selected.update(range(start, end + 1, step))

    if not selected:
        raise InvalidCronExpression(f"{name} selects nothing: {raw!r}")
    return frozenset(selected)


@dataclass(frozen=True)
class CronExpression:
    """A parsed five-field cron cadence, evaluated to the minute."""

    source: str
    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    #: Standard cron unions the two day fields when both are restricted, and
    #: applies only the restricted one otherwise. Retaining which fields were
    #: wildcards is what makes that distinction possible.
    day_of_month_restricted: bool
    day_of_week_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        """Parse one cadence, refusing anything outside the supported subset."""

        fields = expression.split()
        if len(fields) != len(_FIELD_BOUNDS):
            raise InvalidCronExpression(
                f"expected {len(_FIELD_BOUNDS)} fields, got {len(fields)}: "
                f"{expression!r}"
            )

        values = [
            _parse_field(raw, name, low, high)
            for raw, (name, low, high) in zip(fields, _FIELD_BOUNDS, strict=True)
        ]
        minute, hour, day_of_month, month, day_of_week = values

        # Both spellings of Sunday select the same day.
        if 7 in day_of_week:
            day_of_week = frozenset(day_of_week - {7} | {0})

        return cls(
            source=expression,
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month=month,
            day_of_week=day_of_week,
            day_of_month_restricted=fields[2] != "*",
            day_of_week_restricted=fields[4] != "*",
        )

    def matches(self, moment: datetime) -> bool:
        """Return whether one wall-clock minute falls on this cadence.

        Seconds are ignored: cron granularity is one minute, so any tick
        within a selected minute is on cadence. The caller supplies local
        wall-clock time; converting to it is the schedule's job.
        """

        if moment.minute not in self.minute or moment.hour not in self.hour:
            return False
        if moment.month not in self.month:
            return False

        # Python weekday() is Monday 0; cron is Sunday 0.
        weekday = (moment.weekday() + 1) % 7
        by_month_day = moment.day in self.day_of_month
        by_week_day = weekday in self.day_of_week

        if self.day_of_month_restricted and self.day_of_week_restricted:
            return by_month_day or by_week_day
        return by_month_day and by_week_day


@dataclass(frozen=True)
class ScheduleDefinition:
    """One configured recurring cadence for a workflow and store (FR-008)."""

    workflow_name: str
    store_code: str
    cron_expression: str
    timezone: str
    enabled: bool
    #: Derived in __post_init__ so an unrunnable schedule cannot be constructed
    #: and then fail silently at its first tick.
    cadence: CronExpression = field(init=False, compare=False, repr=False)
    zone: ZoneInfo = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("workflow_name", self.workflow_name),
            ("store_code", self.store_code),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")

        object.__setattr__(self, "cadence", CronExpression.parse(self.cron_expression))
        try:
            object.__setattr__(self, "zone", ZoneInfo(self.timezone))
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone: {self.timezone!r}") from error

    def is_due(self, instant: datetime) -> bool:
        """Return whether this cadence selects the given instant.

        The instant is converted to the schedule's own timezone first, so a
        store's cadence follows its local clock rather than the server's.
        """

        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        return self.cadence.matches(instant.astimezone(self.zone))


@dataclass(frozen=True)
class ScheduledLaunchDecision:
    """Whether a schedule launches at one instant, and why not when it does not."""

    should_launch: bool
    refusal: LaunchRefusalReason | None

    def __post_init__(self) -> None:
        if self.should_launch and self.refusal is not None:
            raise ValueError("a launching decision carries no refusal reason")
        if not self.should_launch and self.refusal is None:
            raise ValueError("a refused launch must state a reason")


def decide_scheduled_launch(
    schedule: ScheduleDefinition, instant: datetime
) -> ScheduledLaunchDecision:
    """Decide whether one schedule launches at one instant (FR-008).

    Disabled is checked before timing so a disabled schedule is always refused
    for being disabled. Reporting it as merely not due would hide the operator
    action that stopped it.
    """

    if not schedule.enabled:
        return ScheduledLaunchDecision(
            should_launch=False, refusal=LaunchRefusalReason.SCHEDULE_DISABLED
        )
    if not schedule.is_due(instant):
        return ScheduledLaunchDecision(
            should_launch=False, refusal=LaunchRefusalReason.NOT_DUE
        )
    return ScheduledLaunchDecision(should_launch=True, refusal=None)


@dataclass(frozen=True)
class ManualLaunch:
    """An operator-initiated launch, which must name who and why (FR-008).

    Authorization of that operator is FR-023 and belongs to #26. This type
    establishes only that an identity and a reason exist and are recorded.
    """

    requested_by: str
    reason: str

    def __post_init__(self) -> None:
        for name, value in (
            ("requested_by", self.requested_by),
            ("reason", self.reason),
        ):
            if not value.strip():
                raise InvalidManualLaunch(f"{name} must not be blank")


def build_manual_execution(
    launch: ManualLaunch,
    *,
    workflow_name: str,
    store_code: str,
    mode: ExecutionMode,
    correlation_id: UUID,
    source_window_start: datetime,
    source_window_end: datetime,
    configuration_version_id: UUID,
    rule_version: str,
) -> NewExecution:
    """Build the execution scope for an operator-initiated run (FR-008)."""

    return NewExecution(
        workflow_name=workflow_name,
        store_code=store_code,
        trigger_type=TriggerType.MANUAL,
        mode=mode,
        correlation_id=correlation_id,
        source_window_start=source_window_start,
        source_window_end=source_window_end,
        configuration_version_id=configuration_version_id,
        rule_version=rule_version,
        requested_by=launch.requested_by,
        reason=launch.reason,
    )


def build_scheduled_execution(
    schedule: ScheduleDefinition,
    *,
    mode: ExecutionMode,
    correlation_id: UUID,
    source_window_start: datetime,
    source_window_end: datetime,
    configuration_version_id: UUID,
    rule_version: str,
) -> NewExecution:
    """Build the execution scope for a timed run (FR-008).

    A timed run has no requester, so none is fabricated: the launch source is
    carried by the trigger type instead.
    """

    if not schedule.enabled:
        raise ValueError("a disabled schedule cannot launch an execution")

    return NewExecution(
        workflow_name=schedule.workflow_name,
        store_code=schedule.store_code,
        trigger_type=TriggerType.SCHEDULED,
        mode=mode,
        correlation_id=correlation_id,
        source_window_start=source_window_start,
        source_window_end=source_window_end,
        configuration_version_id=configuration_version_id,
        rule_version=rule_version,
    )


# --- the reproducible window of a scheduled run (FR-002, #28) ----------------

#: How far back a cadence is walked for its previous instant. A year covers
#: every cadence the five-field subset can express except a leap-day one.
PREVIOUS_INSTANT_SEARCH_LIMIT = timedelta(days=366)


class NoPreviousCadenceInstant(ValueError):
    """Raised when a cadence has no earlier instant within the search limit."""


def previous_cadence_instant(schedule: ScheduleDefinition, instant: datetime) -> datetime:
    """Return the cadence instant immediately before ``instant``.

    The owner's decision (2026-09-02): a scheduled run's source window runs
    from the previous instant on its own cadence to the instant it launches
    at. The walk is minute by minute in the schedule's timezone, so the night
    gap of ``*/30 7-23`` is crossed correctly and a replay recomputes the same
    window from the cron expression alone. Seconds are zeroed so consecutive
    windows abut exactly.
    """

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    local = instant.astimezone(schedule.zone).replace(second=0, microsecond=0)
    if not schedule.cadence.matches(local):
        raise ValueError(f"{instant.isoformat()} is not on the cadence {schedule.cron_expression!r}")

    limit = local - PREVIOUS_INSTANT_SEARCH_LIMIT
    candidate = local - timedelta(minutes=1)
    while candidate >= limit:
        if schedule.cadence.matches(candidate):
            return candidate.astimezone(instant.tzinfo)
        candidate -= timedelta(minutes=1)
    raise NoPreviousCadenceInstant(
        f"no instant of {schedule.cron_expression!r} within "
        f"{PREVIOUS_INSTANT_SEARCH_LIMIT.days} days before {instant.isoformat()}"
    )


def scheduled_source_window(
    schedule: ScheduleDefinition, instant: datetime
) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` for the run a schedule launches at ``instant``."""

    end = instant.replace(second=0, microsecond=0)
    return previous_cadence_instant(schedule, instant), end
