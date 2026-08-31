"""Liveness, readiness, and dependency health (FR-024).

The three questions are deliberately distinct: is the process alive, can it
safely accept new work, and is any dependency degraded. A degraded optional
dependency must not make the service claim it is dead, and a health report
must never carry a secret.
"""

import pytest

from esl_service.runtime.health import (
    DependencyHealth,
    HealthReport,
    HealthService,
    HealthState,
)


class StubProbe:
    """A dependency probe with a fixed result, for rule tests."""

    def __init__(
        self,
        name: str,
        state: HealthState,
        *,
        required: bool = True,
        detail: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.required = required
        self._state = state
        self._detail = detail
        self._raises = raises

    def check(self) -> DependencyHealth:
        if self._raises is not None:
            raise self._raises
        return DependencyHealth(
            name=self.name,
            state=self._state,
            required=self.required,
            detail=self._detail,
        )


def service(*probes: StubProbe, configuration_valid: bool = True) -> HealthService:
    """Build a health service over stub probes."""

    return HealthService(
        probes=probes,
        configuration_problems=() if configuration_valid else ("database_url",),
    )


# --- the three distinct questions -------------------------------------------


def test_liveness_is_true_while_the_process_runs() -> None:
    """Liveness answers only whether the process is running (FR-024)."""

    assert service(StubProbe("state-store", HealthState.UNAVAILABLE)).liveness() is True


def test_readiness_is_false_when_a_required_dependency_is_unavailable() -> None:
    """A service that cannot reach its state store must not accept work."""

    health = service(StubProbe("state-store", HealthState.UNAVAILABLE))
    assert health.readiness() is False
    assert health.liveness() is True


def test_readiness_is_true_when_every_required_dependency_is_healthy() -> None:
    """All required dependencies healthy and configuration valid means ready."""

    assert service(StubProbe("state-store", HealthState.HEALTHY)).readiness() is True


def test_degraded_optional_dependency_does_not_block_readiness() -> None:
    """A degraded optional dependency is reported, not fatal."""

    health = service(
        StubProbe("state-store", HealthState.HEALTHY),
        StubProbe("aims-api", HealthState.DEGRADED, required=False),
    )
    assert health.readiness() is True
    assert health.report().state is HealthState.DEGRADED


def test_degraded_required_dependency_blocks_readiness() -> None:
    """A degraded required dependency cannot safely accept new work."""

    assert service(StubProbe("state-store", HealthState.DEGRADED)).readiness() is False


def test_report_distinguishes_all_three_answers() -> None:
    """One report answers alive, ready, and per-dependency state."""

    report = service(
        StubProbe("state-store", HealthState.HEALTHY),
        StubProbe("aims-api", HealthState.UNAVAILABLE, required=False),
    ).report()

    assert isinstance(report, HealthReport)
    assert report.alive is True
    assert report.ready is True
    assert {item.name: item.state for item in report.dependencies} == {
        "state-store": HealthState.HEALTHY,
        "aims-api": HealthState.UNAVAILABLE,
    }


# --- configuration gates readiness (FR-025) ---------------------------------


def test_invalid_configuration_prevents_readiness() -> None:
    """Invalid configuration must stop the service accepting work."""

    health = service(
        StubProbe("state-store", HealthState.HEALTHY), configuration_valid=False
    )
    assert health.readiness() is False
    assert health.liveness() is True


def test_report_names_the_configuration_key_only() -> None:
    """A configuration problem identifies the key, never its value."""

    report = service(
        StubProbe("state-store", HealthState.HEALTHY), configuration_valid=False
    ).report()

    assert report.configuration_problems == ("database_url",)
    assert "postgresql" not in str(report)


# --- no secrets, and a failing probe is contained ----------------------------


def test_probe_failure_is_reported_as_unavailable() -> None:
    """A probe that raises marks its dependency unavailable, not the process."""

    health = service(
        StubProbe("state-store", HealthState.HEALTHY, raises=RuntimeError("boom"))
    )
    report = health.report()

    assert report.alive is True
    assert report.ready is False
    assert report.dependencies[0].state is HealthState.UNAVAILABLE


def test_probe_failure_detail_never_leaks_its_message() -> None:
    """An exception message can contain a connection string, so it is dropped."""

    secret = "postgresql://user:pw@host/db is unreachable"
    report = service(
        StubProbe("state-store", HealthState.HEALTHY, raises=RuntimeError(secret))
    ).report()

    assert "postgresql" not in str(report)
    assert "pw@host" not in str(report)


def test_dependency_detail_rejects_a_secret_like_value() -> None:
    """Detail is operator-facing text and may never carry a credential."""

    with pytest.raises(ValueError, match="detail"):
        DependencyHealth(
            name="state-store",
            state=HealthState.UNAVAILABLE,
            required=True,
            detail="password=hunter2",
        )
