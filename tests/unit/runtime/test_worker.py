"""The worker loop that hands runnable executions to the runner (#102).

The loop picks runnable executions, runs them one at a time per worker
under a concurrency bound, and honours the host lifecycle: paused it picks
nothing, stopped it finishes the run in flight within the deadline. It is
the "checkpoint in-flight work" #28 left open: a stop waits for the current
step boundary, which the runner's checkpoints make safe.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from esl_service.application.runner import RunOutcome
from esl_service.domain.workflow import ExecutionStatus
from esl_service.runtime.service_host import ServiceHost, ServiceState
from esl_service.runtime.worker import WorkerLoop


def outcome(execution_id: UUID, status: ExecutionStatus = ExecutionStatus.SUCCEEDED) -> RunOutcome:
    return RunOutcome(execution_id=execution_id, status=status, terminal_reason=None, steps=(), skipped_steps=(), retry_after_seconds=None, persisted=None)


@dataclass
class FakeQueue:
    pending: list[UUID] = field(default_factory=list)
    ran: list[UUID] = field(default_factory=list)
    running: int = 0
    peak: int = 0
    hold: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def pick(self, limit: int) -> list[UUID]:
        with self.lock:
            taken, self.pending = self.pending[:limit], self.pending[limit:]
        return taken

    def run(self, execution_id: UUID) -> RunOutcome:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.hold)
            with self.lock:
                self.ran.append(execution_id)
            return outcome(execution_id)
        finally:
            with self.lock:
                self.running -= 1


def wait_until(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.005)
    return False


def test_the_loop_runs_every_pending_execution_under_the_concurrency_bound() -> None:
    queue = FakeQueue(pending=[uuid4() for _ in range(6)], hold=0.03)
    loop = WorkerLoop(queue.pick, queue.run, concurrency=2, interval_seconds=0.01)

    loop.start()
    try:
        assert wait_until(lambda: len(queue.ran) == 6)
    finally:
        assert loop.stop(deadline_seconds=2) is True
    assert 1 < queue.peak <= 2


def test_a_paused_loop_picks_nothing_until_resumed() -> None:
    queue = FakeQueue(pending=[uuid4()])
    loop = WorkerLoop(queue.pick, queue.run, concurrency=1, interval_seconds=0.01)
    loop.pause()

    loop.start()
    try:
        time.sleep(0.1)
        assert queue.ran == []
        loop.resume()
        assert wait_until(lambda: len(queue.ran) == 1)
    finally:
        loop.stop(deadline_seconds=2)


def test_stop_waits_for_the_run_in_flight_within_the_deadline() -> None:
    queue = FakeQueue(pending=[uuid4()], hold=0.2)
    loop = WorkerLoop(queue.pick, queue.run, concurrency=1, interval_seconds=0.01)

    loop.start()
    assert wait_until(lambda: queue.running == 1)
    stopped = loop.stop(deadline_seconds=2)

    assert stopped is True
    assert len(queue.ran) == 1


def test_stop_reports_a_missed_deadline_instead_of_hanging() -> None:
    queue = FakeQueue(pending=[uuid4()], hold=0.5)
    loop = WorkerLoop(queue.pick, queue.run, concurrency=1, interval_seconds=0.01)

    loop.start()
    assert wait_until(lambda: queue.running == 1)
    stopped = loop.stop(deadline_seconds=0.05)

    assert stopped is False
    assert wait_until(lambda: len(queue.ran) == 1, timeout=2)  # it still finishes; nothing is killed


def test_an_error_in_one_run_does_not_stop_the_loop() -> None:
    errors: list[BaseException] = []
    bad, good = uuid4(), uuid4()
    queue = FakeQueue(pending=[bad, good])

    def run(execution_id: UUID) -> RunOutcome:
        if execution_id == bad:
            raise RuntimeError("state store went away")
        return queue.run(execution_id)

    loop = WorkerLoop(queue.pick, run, concurrency=1, interval_seconds=0.01, on_error=errors.append)
    loop.start()
    try:
        assert wait_until(lambda: queue.ran == [good])
    finally:
        loop.stop(deadline_seconds=2)
    assert len(errors) == 1


@pytest.mark.parametrize("concurrency", [0, -2])
def test_a_non_positive_concurrency_is_refused(concurrency: int) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        WorkerLoop(FakeQueue().pick, FakeQueue().run, concurrency=concurrency, interval_seconds=0.01)


# --- the host controls the worker like the scheduler -----------------------------------


@dataclass
class FakeScheduler:
    paused: bool = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


@dataclass
class FakeTicker:
    events: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.events.append("start")

    def stop(self, *, deadline_seconds: float) -> bool:
        self.events.append("stop")
        return True


@dataclass
class FakeWorker(FakeTicker):
    paused: bool = False

    def pause(self) -> None:
        self.events.append("pause")
        self.paused = True

    def resume(self) -> None:
        self.events.append("resume")
        self.paused = False


class NoAudit:
    def append_audit_entry(self, **fields: object) -> None:
        return None


def test_the_host_starts_pauses_resumes_and_stops_the_worker_with_the_scheduler() -> None:
    worker = FakeWorker()
    host = ServiceHost(scheduler=FakeScheduler(), ticker=FakeTicker(), audit=NoAudit(), worker=worker, clock=lambda: datetime(2026, 9, 2, tzinfo=UTC))

    host.start()
    host.pause(reason="p")
    assert worker.paused is True
    host.resume(reason="r")
    assert worker.paused is False
    host.stop(reason="s")

    assert host.state is ServiceState.STOPPED
    assert worker.events == ["start", "pause", "resume", "stop"]
