"""Service lifecycle: start, pause, resume, stop, with quiesce (FR-029, #28).

``ServiceHost`` is the pure lifecycle the Windows Service wrapper delegates
to, so Service Control Manager semantics are testable without pywin32. The
order of operations is the point:

- ``start`` resumes the scheduler and starts the once-a-minute ticker;
- ``pause`` pauses the scheduler and leaves the ticker running, so no new
  run launches while the process stays alive and answers status;
- ``stop`` pauses the scheduler *first*, then stops the ticker within the
  configured deadline, so nothing launches during shutdown.

Every transition is a lifecycle audit entry under the ``service`` actor. The
ledger may be unreachable at exactly the moment a stop is requested, so a
failed audit never blocks a transition; the missed action is kept in
``unrecorded_transitions`` for the operator to see.

Checkpointing in-flight work is not part of this yet: no workflow runner
exists (adapters #91 to #94 are open), so an execution launched by the
scheduler has no step to checkpoint. When a runner arrives it hooks into the
same ``stop`` deadline.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from esl_service.application.operations import AuditPort
from esl_service.domain.serialization import JSONValue

SERVICE_ACTOR = "service"
SERVICE_RESOURCE = "service_lifecycle"
SERVICE_STARTED = "service.started"
SERVICE_PAUSED = "service.paused"
SERVICE_RESUMED = "service.resumed"
SERVICE_STOPPED = "service.stopped"


class ServiceState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


class InvalidLifecycleTransition(RuntimeError):
    """Raised when a Service Control Manager request does not fit the current state."""


class Quiescable(Protocol):
    def pause(self) -> None: ...

    def resume(self) -> None: ...


class Ticker(Protocol):
    """The once-a-minute loop the host owns."""

    def start(self) -> None: ...

    def stop(self, *, deadline_seconds: float) -> bool: ...


class ServiceHost:
    """The lifecycle state machine behind the Windows Service."""

    def __init__(
        self,
        *,
        scheduler: Quiescable,
        ticker: Ticker,
        audit: AuditPort,
        stop_deadline_seconds: float = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._scheduler = scheduler
        self._ticker = ticker
        self._audit = audit
        self._deadline = stop_deadline_seconds
        self._clock = clock
        self._state = ServiceState.STOPPED
        self._unrecorded: list[str] = []
        # Nothing launches before start is explicitly requested.
        self._scheduler.pause()

    @property
    def state(self) -> ServiceState:
        return self._state

    @property
    def unrecorded_transitions(self) -> tuple[str, ...]:
        """Lifecycle actions whose audit entry could not be written."""

        return tuple(self._unrecorded)

    # -- transitions ---------------------------------------------------------

    def start(self) -> None:
        self._require(ServiceState.STOPPED, "start")
        self._scheduler.resume()
        self._ticker.start()
        self._state = ServiceState.RUNNING
        self._record(SERVICE_STARTED, "service start", {"state": self._state.value})

    def pause(self, *, reason: str) -> None:
        self._require(ServiceState.RUNNING, "pause")
        self._scheduler.pause()
        self._state = ServiceState.PAUSED
        self._record(SERVICE_PAUSED, reason, {"state": self._state.value})

    def resume(self, *, reason: str) -> None:
        self._require(ServiceState.PAUSED, "resume")
        self._scheduler.resume()
        self._state = ServiceState.RUNNING
        self._record(SERVICE_RESUMED, reason, {"state": self._state.value})

    def stop(self, *, reason: str) -> None:
        if self._state is ServiceState.STOPPED:
            raise InvalidLifecycleTransition("cannot stop: the service is already stopped")
        self._scheduler.pause()
        stopped_in_time = self._ticker.stop(deadline_seconds=self._deadline)
        self._state = ServiceState.STOPPED
        self._record(
            SERVICE_STOPPED,
            reason,
            {"state": self._state.value, "ticker_stopped_in_time": stopped_in_time},
        )

    # -- helpers -------------------------------------------------------------

    def _require(self, expected: ServiceState, request: str) -> None:
        if self._state is not expected:
            raise InvalidLifecycleTransition(
                f"cannot {request}: the service is {self._state.value}, not {expected.value}"
            )

    def _record(self, action: str, reason: str, evidence: dict[str, JSONValue]) -> None:
        try:
            self._audit.append_audit_entry(
                actor=SERVICE_ACTOR,
                action=action,
                reason=reason,
                resource_type=SERVICE_RESOURCE,
                resource_key=SERVICE_RESOURCE,
                outcome="APPLIED",
                after_evidence={**evidence, "at": self._clock().isoformat()},
            )
        except Exception:  # noqa: BLE001 - the ledger must not block a lifecycle change
            self._unrecorded.append(action)
