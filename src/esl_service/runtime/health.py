"""Liveness, readiness, and dependency health without secrets (FR-024).

Three questions are kept deliberately distinct:

* **liveness** — is the process running? A dead dependency never makes a
  running process report itself dead, because that would cause a supervisor to
  restart a service whose only problem is external.
* **readiness** — can new work be accepted safely? Invalid configuration or an
  unhealthy required dependency both answer no (FR-025).
* **dependency health** — which external system is degraded, named without
  leaking how to reach it.

A probe that raises is contained: its dependency is reported UNAVAILABLE and
the exception message is dropped, because a connection error commonly embeds a
connection string.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from esl_service.domain.serialization import FORBIDDEN_EVIDENCE_KEY_FRAGMENTS


class HealthState(StrEnum):
    """Observed state of one dependency, or of the service as a whole."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


#: Detail text replacing a probe failure, since the original may hold a secret.
UNAVAILABLE_DETAIL = "dependency check failed"


@dataclass(frozen=True)
class DependencyHealth:
    """One dependency's observed state, safe to show an operator."""

    name: str
    state: HealthState
    required: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must not be blank")
        if self.detail is None:
            return
        lowered = self.detail.casefold()
        for fragment in FORBIDDEN_EVIDENCE_KEY_FRAGMENTS:
            if fragment in lowered:
                raise ValueError(
                    f"detail must not contain the secret-like term {fragment!r}"
                )


class DependencyProbe(Protocol):
    """Checks one external dependency the service relies on."""

    name: str
    required: bool

    def check(self) -> DependencyHealth: ...


@dataclass(frozen=True)
class HealthReport:
    """One answer to all three health questions."""

    alive: bool
    ready: bool
    dependencies: tuple[DependencyHealth, ...]
    configuration_problems: tuple[str, ...] = field(default=())

    @property
    def state(self) -> HealthState:
        """Return the worst state across dependencies."""

        states = {item.state for item in self.dependencies}
        if HealthState.UNAVAILABLE in states:
            return HealthState.UNAVAILABLE
        if HealthState.DEGRADED in states:
            return HealthState.DEGRADED
        return HealthState.HEALTHY


class HealthService:
    """Answers liveness, readiness, and dependency health for one process."""

    def __init__(
        self,
        probes: Sequence[DependencyProbe],
        configuration_problems: Sequence[str] = (),
    ) -> None:
        self._probes = tuple(probes)
        self._configuration_problems = tuple(configuration_problems)

    def liveness(self) -> bool:
        """Return whether the process itself is running.

        Always true when reachable: an external outage must not be reported as
        a dead process.
        """

        return True

    def readiness(self) -> bool:
        """Return whether new work can be accepted safely."""

        return self.report().ready

    def dependency_health(self) -> tuple[DependencyHealth, ...]:
        """Return each dependency's state, containing any probe failure."""

        return tuple(self._check(probe) for probe in self._probes)

    def report(self) -> HealthReport:
        """Return liveness, readiness, and dependency health together."""

        dependencies = self.dependency_health()
        ready = not self._configuration_problems and all(
            item.state is HealthState.HEALTHY
            for item in dependencies
            if item.required
        )
        return HealthReport(
            alive=self.liveness(),
            ready=ready,
            dependencies=dependencies,
            configuration_problems=self._configuration_problems,
        )

    @staticmethod
    def _check(probe: DependencyProbe) -> DependencyHealth:
        """Run one probe, converting any failure into an unavailable result."""

        try:
            return probe.check()
        except Exception:  # noqa: BLE001
            # The message is deliberately discarded: a connection error
            # commonly embeds the connection string that caused it.
            return DependencyHealth(
                name=probe.name,
                state=HealthState.UNAVAILABLE,
                required=probe.required,
                detail=UNAVAILABLE_DETAIL,
            )
