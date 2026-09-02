"""The reproducible source window of a scheduled run (FR-002, FR-008, #28).

The owner decided on 2026-09-02 that a scheduled run's window runs from the
previous instant on its own cadence up to the instant it launches at. That
is derived from the cron expression alone, so a replay of the same schedule
instant recomputes the same window without any stored state.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from esl_service.domain.scheduling import (
    NoPreviousCadenceInstant,
    ScheduleDefinition,
    previous_cadence_instant,
    scheduled_source_window,
)

LEGACY_CADENCE = "*/30 7-23 * * *"


def jakarta(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Jakarta")).astimezone(
        UTC
    )


def definition(cron: str = LEGACY_CADENCE, tz: str = "Asia/Jakarta") -> ScheduleDefinition:
    return ScheduleDefinition(
        workflow_name="esl-refresh",
        store_code="084",
        cron_expression=cron,
        timezone=tz,
        enabled=True,
    )


def test_the_previous_instant_is_one_cadence_step_earlier() -> None:
    assert previous_cadence_instant(definition(), jakarta(2026, 8, 31, 7, 30)) == jakarta(
        2026, 8, 31, 7, 0
    )


def test_the_first_instant_of_a_day_reaches_back_across_the_night_gap() -> None:
    """07:00's previous cadence instant is 23:30 the day before, not 06:30."""

    assert previous_cadence_instant(definition(), jakarta(2026, 8, 31, 7, 0)) == jakarta(
        2026, 8, 30, 23, 30
    )


def test_the_window_is_evaluated_in_the_schedule_timezone() -> None:
    """A UTC instant is converted to Asia/Jakarta before the cadence is walked."""

    instant = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)  # 07:30 Jakarta

    assert previous_cadence_instant(definition(), instant) == jakarta(2026, 8, 31, 7, 0)


def test_the_result_carries_seconds_zeroed_so_windows_abut_exactly() -> None:
    instant = jakarta(2026, 8, 31, 7, 30).replace(second=17, microsecond=250)

    start = previous_cadence_instant(definition(), instant)

    assert start.second == 0 and start.microsecond == 0


def test_an_instant_off_the_cadence_is_refused() -> None:
    with pytest.raises(ValueError, match="not on the cadence"):
        previous_cadence_instant(definition(), jakarta(2026, 8, 31, 7, 15))


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        previous_cadence_instant(definition(), datetime(2026, 8, 31, 7, 30))  # noqa: DTZ001


def test_a_cadence_with_no_instant_within_the_search_bound_is_refused() -> None:
    """A leap-day cadence has no previous instant within a year; refuse, do not spin."""

    leap_day = definition(cron="0 0 29 2 *", tz="UTC")

    with pytest.raises(NoPreviousCadenceInstant):
        previous_cadence_instant(leap_day, datetime(2028, 2, 29, 0, 0, tzinfo=UTC))


def test_the_source_window_ends_at_the_launch_instant_with_seconds_zeroed() -> None:
    instant = jakarta(2026, 8, 31, 7, 30).replace(second=41)

    start, end = scheduled_source_window(definition(), instant)

    assert (start, end) == (jakarta(2026, 8, 31, 7, 0), jakarta(2026, 8, 31, 7, 30))
