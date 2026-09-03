"""The worker loop that hands runnable executions to the runner (#102, FR-029).

A single loop thread picks runnable executions and hands each to a bounded
pool that calls the runner. It honours the host lifecycle the same way the
scheduler tick does: paused, it picks nothing; stopped, it waits for the
runs in flight up to the deadline and reports whether they finished. It
never kills a run: the runner's checkpoints make the next start safe, and
an abandoned run is found by ``recover_all`` on the next start.
"""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from uuid import UUID

from esl_service.application.runner import RunOutcome

Picker = Callable[[int], Sequence[UUID]]
Runner = Callable[[UUID], RunOutcome]


class WorkerLoop:
    """Runs picked executions under a concurrency bound; pausable and stoppable."""

    def __init__(
        self,
        pick: Picker,
        run: Runner,
        *,
        concurrency: int,
        interval_seconds: float,
        on_error: Callable[[BaseException], None] | None = None,
        on_start: Callable[[], object] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._pick = pick
        self._run = run
        self._concurrency = concurrency
        self._interval = interval_seconds
        self._on_error = on_error
        self._on_start = on_start
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: dict[UUID, Future[RunOutcome]] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="esl-run")
        if self._on_start is not None:
            self._guard(self._on_start)
        self._thread = threading.Thread(target=self._loop, name="esl-worker", daemon=True)
        self._thread.start()

    def stop(self, *, deadline_seconds: float) -> bool:
        """Stop picking and wait for the runs in flight; True when all finished in time."""

        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=deadline_seconds)
        with self._lock:
            pending = list(self._in_flight.values())
        finished = wait(pending, timeout=deadline_seconds).not_done == set() if pending else True
        if self._pool is not None:
            self._pool.shutdown(wait=False)
        return finished and not (thread is not None and thread.is_alive())

    # -- the loop ----------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                self._guard(self._dispatch)
            self._stop.wait(self._interval)

    def _dispatch(self) -> None:
        with self._lock:
            self._in_flight = {i: f for i, f in self._in_flight.items() if not f.done()}
            free = self._concurrency - len(self._in_flight)
            busy = set(self._in_flight)
        if free <= 0 or self._pool is None:
            return
        for execution_id in self._pick(free):
            if execution_id in busy:
                continue
            future = self._pool.submit(self._run_one, execution_id)
            with self._lock:
                self._in_flight[execution_id] = future

    def _run_one(self, execution_id: UUID) -> RunOutcome:
        try:
            return self._run(execution_id)
        except BaseException as error:
            if self._on_error is not None:
                self._on_error(error)
            raise

    def _guard(self, action: Callable[[], object]) -> None:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - the loop must survive one bad pick
            if self._on_error is not None:
                self._on_error(error)
