"""Recurring schedules and auditable manual launch (FR-008).

A schedule decides only whether a run is due. It never decides ownership of a
scope, which is FR-009/FR-017 and belongs to the lease, and it never retries or
replays, which is FR-011 and belongs to #16.

The cron subset supported here is deliberately narrow and is rejected rather
than guessed at when unrecognised, so a mistyped expression cannot silently
schedule the wrong thing.
"""

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from esl_service.domain.outcomes import ExecutionMode, TriggerType
from esl_service.domain.scheduling import (
    CronExpression,
    InvalidCronExpression,
    InvalidManualLaunch,
    LaunchRefusalReason,
    ManualLaunch,
    ScheduleDefinition,
    build_manual_execution,
    build_scheduled_execution,
    decide_scheduled_launch,
)

#: The VERIFIED legacy cadence: every 30 minutes from 07:00 through 23:59.
LEGACY_CADENCE = "*/30 7-23 * * *"


def schedule(**overrides: object) -> ScheduleDefinition:
    """Build a schedule, overriding only what a test needs."""

    values: dict[str, object] = {
        "workflow_name": "esl-refresh",
        "store_code": "084",
        "cron_expression": LEGACY_CADENCE,
        "timezone": "Asia/Jakarta",
        "enabled": True,
    }
    values.update(overrides)
    return ScheduleDefinition(**values)  # type: ignore[arg-type]


def jakarta(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Return the UTC instant of one Asia/Jakarta wall-clock time."""

    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Jakarta"))
    return local.astimezone(UTC)


# --- cron parsing is explicit, and refuses what it does not support --------


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        (LEGACY_CADENCE, datetime(2026, 8, 31, 7, 0, tzinfo=UTC), True),
        (LEGACY_CADENCE, datetime(2026, 8, 31, 7, 30, tzinfo=UTC), True),
        (LEGACY_CADENCE, datetime(2026, 8, 31, 23, 30, tzinfo=UTC), True),
        (LEGACY_CADENCE, datetime(2026, 8, 31, 7, 15, tzinfo=UTC), False),
        (LEGACY_CADENCE, datetime(2026, 8, 31, 6, 30, tzinfo=UTC), False),
        (LEGACY_CADENCE, datetime(2026, 8, 31, 0, 0, tzinfo=UTC), False),
        ("0 8 * * *", datetime(2026, 8, 31, 8, 0, tzinfo=UTC), True),
        ("0,15 8 * * *", datetime(2026, 8, 31, 8, 15, tzinfo=UTC), True),
        ("0 8 1 1 *", datetime(2026, 1, 1, 8, 0, tzinfo=UTC), True),
        ("0 8 1 1 *", datetime(2026, 2, 1, 8, 0, tzinfo=UTC), False),
    ],
)
def test_cron_matches_the_expected_minutes(
    expression: str, moment: datetime, expected: bool
) -> None:
    """The legacy 30-minute daytime cadence is expressible and exact."""

    assert CronExpression.parse(expression).matches(moment) is expected


def test_day_of_month_and_day_of_week_together_match_either() -> None:
    """Standard cron unions the two day fields when both are restricted.

    2026-08-31 is a Monday. The expression restricts both day fields, so a
    match on either is a match, which is the documented cron behaviour rather
    than an intersection.
    """

    both_restricted = CronExpression.parse("0 8 15 * 1")

    assert both_restricted.matches(datetime(2026, 8, 31, 8, 0, tzinfo=UTC)) is True
    assert both_restricted.matches(datetime(2026, 8, 15, 8, 0, tzinfo=UTC)) is True
    assert both_restricted.matches(datetime(2026, 8, 18, 8, 0, tzinfo=UTC)) is False


def test_sunday_is_accepted_as_both_zero_and_seven() -> None:
    """Both conventional spellings of Sunday select the same day."""

    sunday = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)

    assert CronExpression.parse("0 8 * * 0").matches(sunday) is True
    assert CronExpression.parse("0 8 * * 7").matches(sunday) is True


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "* * * * * *",
        "@daily",
        "0 8 * * MON",
        "60 8 * * *",
        "0 24 * * *",
        "0 8 32 * *",
        "0 8 * 13 *",
        "0 8 * * 8",
        "*/0 8 * * *",
        "5-1 8 * * *",
        "abc 8 * * *",
    ],
)
def test_unsupported_or_invalid_cron_is_rejected(expression: str) -> None:
    """An expression that cannot be read is refused, never partially guessed."""

    with pytest.raises(InvalidCronExpression):
        CronExpression.parse(expression)


def test_seconds_do_not_affect_a_match() -> None:
    """Cron granularity is one minute, so a tick within the minute matches."""

    assert CronExpression.parse("0 8 * * *").matches(
        datetime(2026, 8, 31, 8, 0, 45, 123, tzinfo=UTC)
    )


# --- a disabled schedule creates no run (FR-008 acceptance) ----------------


def test_a_disabled_schedule_is_never_due() -> None:
    """The first acceptance criterion: a disabled schedule launches nothing."""

    decision = decide_scheduled_launch(
        schedule(enabled=False), jakarta(2026, 8, 31, 7, 30)
    )

    assert decision.should_launch is False
    assert decision.refusal is LaunchRefusalReason.SCHEDULE_DISABLED


def test_a_disabled_schedule_is_refused_for_being_disabled_not_for_timing() -> None:
    """Disabled outranks timing, so the audit reason is never misleading."""

    decision = decide_scheduled_launch(
        schedule(enabled=False), jakarta(2026, 8, 31, 3, 17)
    )

    assert decision.refusal is LaunchRefusalReason.SCHEDULE_DISABLED


def test_an_enabled_schedule_launches_when_due() -> None:
    """An enabled schedule at a matching minute is the launch case."""

    decision = decide_scheduled_launch(
        schedule(), jakarta(2026, 8, 31, 7, 30)
    )

    assert decision.should_launch is True
    assert decision.refusal is None


def test_an_enabled_schedule_outside_its_cadence_is_not_due() -> None:
    """Outside the configured window nothing launches."""

    decision = decide_scheduled_launch(
        schedule(), jakarta(2026, 8, 31, 6, 30)
    )

    assert decision.should_launch is False
    assert decision.refusal is LaunchRefusalReason.NOT_DUE


def test_the_schedule_is_evaluated_in_its_own_timezone() -> None:
    """A store's cadence follows its local clock, not the server's.

    00:30 UTC is 07:30 in Jakarta, which is inside the configured window. If
    the schedule were evaluated in UTC it would be outside it.
    """

    due_locally = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)

    assert decide_scheduled_launch(schedule(), due_locally).should_launch is True


def test_an_instant_without_a_timezone_is_refused() -> None:
    """A naive instant has no defined local time, so it cannot be evaluated."""

    with pytest.raises(ValueError, match="timezone-aware"):
        decide_scheduled_launch(schedule(), datetime(2026, 8, 31, 7, 30))  # noqa: DTZ001


def test_an_unknown_timezone_is_refused_at_definition_time() -> None:
    """A schedule that cannot be evaluated must not be storable."""

    with pytest.raises(ValueError, match="timezone"):
        schedule(timezone="Mars/Olympus")


def test_a_schedule_validates_its_cron_when_defined() -> None:
    """An unreadable cadence fails when configured, not at the first tick."""

    with pytest.raises(InvalidCronExpression):
        schedule(cron_expression="@hourly")


# --- a manual launch carries identity and reason (FR-008 acceptance) -------


def test_a_manual_launch_requires_an_identity() -> None:
    """Second acceptance criterion: an anonymous manual run is not allowed."""

    with pytest.raises(InvalidManualLaunch, match="requested_by"):
        ManualLaunch(requested_by="  ", reason="INC-1234 price correction")


def test_a_manual_launch_requires_a_reason() -> None:
    """A manual run without a reason leaves an unauditable trail."""

    with pytest.raises(InvalidManualLaunch, match="reason"):
        ManualLaunch(requested_by="ops.alice", reason="")


def test_a_manual_execution_records_identity_reason_and_source() -> None:
    """The created execution carries who, why, and that it was manual."""

    execution = build_manual_execution(
        ManualLaunch(requested_by="ops.alice", reason="INC-1234 price correction"),
        workflow_name="esl-refresh",
        store_code="084",
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
        source_window_end=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
        configuration_version_id=uuid4(),
        rule_version="compatibility-v1",
    )

    assert execution.trigger_type is TriggerType.MANUAL
    assert execution.requested_by == "ops.alice"
    assert execution.reason == "INC-1234 price correction"


def test_a_scheduled_execution_records_its_schedule_as_the_source() -> None:
    """Launch source is distinguishable in the execution record itself."""

    execution = build_scheduled_execution(
        schedule(),
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
        source_window_end=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
        configuration_version_id=uuid4(),
        rule_version="compatibility-v1",
    )

    assert execution.trigger_type is TriggerType.SCHEDULED
    assert execution.workflow_name == "esl-refresh"
    assert execution.store_code == "084"


def test_a_scheduled_execution_names_no_operator() -> None:
    """A timed run has no requester, so it must not fabricate one."""

    execution = build_scheduled_execution(
        schedule(),
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
        source_window_end=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
        configuration_version_id=uuid4(),
        rule_version="compatibility-v1",
    )

    assert execution.requested_by is None


def test_a_disabled_schedule_cannot_build_an_execution() -> None:
    """Nothing may construct a run from a schedule that must not launch."""

    with pytest.raises(ValueError, match="disabled"):
        build_scheduled_execution(
            schedule(enabled=False),
            mode=ExecutionMode.SHADOW,
            correlation_id=uuid4(),
            source_window_start=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
            source_window_end=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
            configuration_version_id=uuid4(),
            rule_version="compatibility-v1",
        )
