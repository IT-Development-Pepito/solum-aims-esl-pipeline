"""Service lifecycle: start, pause, resume, stop, with quiesce (FR-029, #28).

``ServiceHost`` is the pure lifecycle the Windows Service wrapper delegates
to, so Service Control Manager semantics are testable without pywin32. Pause
and stop quiesce scheduling first: no new run is launched once the request
is received, and the tick loop is told to stop before the state changes.
Every transition is a lifecycle audit entry under the ``service`` actor, so
an operator can see from the ledger when the host was paused and by what.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from esl_service.runtime.service_host import (
    SERVICE_ACTOR,
    SERVICE_RESOURCE,
    InvalidLifecycleTransition,
    ServiceHost,
    ServiceState,
)

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


@dataclass
class FakeScheduler:
    paused: bool = False
    events: list[str] = field(default_factory=list)

    def pause(self) -> None:
        self.paused = True
        self.events.append("pause")

    def resume(self) -> None:
        self.paused = False
        self.events.append("resume")


@dataclass
class FakeTicker:
    """The once-a-minute loop the host owns."""

    running: bool = False
    events: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.running = True
        self.events.append("start")

    def stop(self, *, deadline_seconds: float) -> bool:
        self.running = False
        self.events.append(f"stop:{deadline_seconds:g}")
        return True


@dataclass
class FakeAudit:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append_audit_entry(self, **fields: Any) -> dict[str, Any]:
        self.entries.append(fields)
        return fields

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]


@dataclass
class Harness:
    scheduler: FakeScheduler
    ticker: FakeTicker
    audit: FakeAudit
    host: ServiceHost


@pytest.fixture
def harness() -> Harness:
    scheduler, ticker, audit = FakeScheduler(), FakeTicker(), FakeAudit()
    host = ServiceHost(
        scheduler=scheduler,
        ticker=ticker,
        audit=audit,
        stop_deadline_seconds=30,
        clock=lambda: NOW,
    )
    return Harness(scheduler, ticker, audit, host)


def test_a_new_host_is_stopped_and_its_scheduler_paused(harness: Harness) -> None:
    """Nothing launches before start is explicitly requested."""

    assert harness.host.state is ServiceState.STOPPED
    assert harness.scheduler.paused is True


def test_start_resumes_the_scheduler_starts_the_ticker_and_audits(harness: Harness) -> None:
    harness.host.start()

    assert harness.host.state is ServiceState.RUNNING
    assert harness.scheduler.paused is False
    assert harness.ticker.running is True
    (entry,) = harness.audit.entries
    assert entry["action"] == "service.started"
    assert entry["actor"] == SERVICE_ACTOR
    assert entry["resource_type"] == SERVICE_RESOURCE
    assert entry["outcome"] == "APPLIED"


def test_pause_quiesces_scheduling_but_keeps_the_process_alive(harness: Harness) -> None:
    harness.host.start()

    harness.host.pause(reason="SCM pause")

    assert harness.host.state is ServiceState.PAUSED
    assert harness.scheduler.paused is True
    assert harness.ticker.running is True  # the loop keeps running; it launches nothing
    assert harness.audit.actions()[-1] == "service.paused"
    assert harness.audit.entries[-1]["reason"] == "SCM pause"


def test_resume_reenables_scheduling(harness: Harness) -> None:
    harness.host.start()
    harness.host.pause(reason="SCM pause")

    harness.host.resume(reason="SCM continue")

    assert harness.host.state is ServiceState.RUNNING
    assert harness.scheduler.paused is False
    assert harness.audit.actions()[-1] == "service.resumed"


def test_stop_pauses_scheduling_before_stopping_the_ticker_within_the_deadline(
    harness: Harness,
) -> None:
    harness.host.start()

    harness.host.stop(reason="SCM stop")

    assert harness.host.state is ServiceState.STOPPED
    assert harness.scheduler.events == ["pause", "resume", "pause"]
    assert harness.ticker.events == ["start", "stop:30"]
    assert harness.audit.actions()[-1] == "service.stopped"
    assert harness.audit.entries[-1]["after_evidence"]["ticker_stopped_in_time"] is True


def test_stop_from_paused_is_allowed(harness: Harness) -> None:
    harness.host.start()
    harness.host.pause(reason="p")

    harness.host.stop(reason="s")

    assert harness.host.state is ServiceState.STOPPED


def test_a_ticker_that_misses_the_deadline_is_recorded_not_hidden(harness: Harness) -> None:
    harness.ticker.stop = lambda *, deadline_seconds: False  # type: ignore[method-assign]
    harness.host.start()

    harness.host.stop(reason="s")

    assert harness.host.state is ServiceState.STOPPED
    assert harness.audit.entries[-1]["after_evidence"]["ticker_stopped_in_time"] is False


@pytest.mark.parametrize(
    ("sequence", "bad"),
    [
        ((), "pause"),
        ((), "resume"),
        ((), "stop"),
        (("start",), "start"),
        (("start",), "resume"),
        (("start", "pause"), "pause"),
        (("start", "stop"), "pause"),
    ],
)
def test_an_invalid_transition_is_refused_and_changes_nothing(
    harness: Harness, sequence: tuple[str, ...], bad: str
) -> None:
    for step in sequence:
        getattr(harness.host, step)(**({} if step == "start" else {"reason": step}))
    state_before = harness.host.state
    audited_before = len(harness.audit.entries)

    with pytest.raises(InvalidLifecycleTransition):
        getattr(harness.host, bad)(**({} if bad == "start" else {"reason": bad}))

    assert harness.host.state is state_before
    assert len(harness.audit.entries) == audited_before


def test_an_audit_failure_does_not_block_a_lifecycle_change(harness: Harness) -> None:
    """The store may be down at stop time; stopping must still succeed."""

    def broken(**_: Any) -> None:
        raise RuntimeError("state store unavailable")

    harness.audit.append_audit_entry = broken  # type: ignore[method-assign]
    harness.host.start()

    harness.host.stop(reason="s")

    assert harness.host.state is ServiceState.STOPPED
    assert harness.host.unrecorded_transitions == ("service.started", "service.stopped")
