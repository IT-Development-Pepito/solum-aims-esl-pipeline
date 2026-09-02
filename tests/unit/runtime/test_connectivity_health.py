"""Connectivity is reported through HealthService, not a parallel mechanism (#78).

One DependencyProbe per configured target. Only the state store is required:
a source that is down degrades the report but must not make a running service
report itself unready, because it can still serve status and audit.
"""

from sqlalchemy.engine import URL

from esl_service.config import Settings
from esl_service.runtime.connectivity import (
    ConnectionTarget,
    ConnectivityProbe,
    TargetKind,
    build_probes,
)
from esl_service.runtime.health import HealthService, HealthState
from esl_service.runtime.secrets import SecretUnavailableError


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


class Reachable:
    def connect_and_identify(self, url: URL) -> str:
        return "esl_reader"


class Refused:
    def connect_and_identify(self, url: URL) -> str:
        raise ConnectionRefusedError()


def target(name: str = "aims-portal", host: str = "h") -> ConnectionTarget:
    return ConnectionTarget(
        name=name,
        kind=TargetKind.POSTGRESQL,
        host=host,
        port=5432,
        database="db",
        username="u",
        password_key="aims.portal.password",
    )


# --- one probe, mapped onto the health vocabulary --------------------------


def test_a_reachable_target_is_healthy() -> None:
    probe = ConnectivityProbe(target(), StaticSecrets({"aims.portal.password": "x"}), Reachable())

    health = probe.check()

    assert probe.name == "aims-portal"
    assert health.state is HealthState.HEALTHY
    assert health.detail is None


def test_an_unreachable_target_is_unavailable_with_a_fixed_detail() -> None:
    probe = ConnectivityProbe(target(), StaticSecrets({"aims.portal.password": "x"}), Refused())

    health = probe.check()

    assert health.state is HealthState.UNAVAILABLE
    assert health.detail == "no answer from the host, port, or database"


def test_a_missing_secret_is_unavailable_and_says_so() -> None:
    health = ConnectivityProbe(target(), StaticSecrets({}), Reachable()).check()

    assert health.state is HealthState.UNAVAILABLE
    assert health.detail is not None
    assert "bundle" in health.detail


def test_an_unconfigured_target_is_degraded_not_unavailable() -> None:
    """A gap in configuration is visible without being counted as an outage."""

    health = ConnectivityProbe(target(host=""), StaticSecrets({}), Reachable()).check()

    assert health.state is HealthState.DEGRADED


def test_sources_are_not_required_but_the_state_store_is() -> None:
    settings = Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql+psycopg://u@localhost/esl",
            "internal_host": "127.0.0.1",
            "aims_host": "aims.internal",
            "aims_portal_database": "AIMS_PORTAL_DB",
            "aims_portal_username": "u",
        }
    )

    probes = {probe.name: probe for probe in build_probes(settings, StaticSecrets({}), Reachable())}

    assert probes["state-store"].required is True
    assert probes["aims-portal"].required is False


# --- wired into the existing service -------------------------------------


def test_a_down_source_is_reported_but_leaves_the_service_ready() -> None:
    """Readiness is about accepting work safely; a source outage does not deny it.

    The aggregate state still reflects the worst dependency, which is the
    existing #27 rule and is deliberately not changed here: an operator must
    see the outage even though the service keeps accepting work.
    """

    secrets = StaticSecrets({"aims.portal.password": "x", "state.password": "y"})
    state = ConnectivityProbe(
        ConnectionTarget("state-store", TargetKind.POSTGRESQL, "h", 5432, "esl", "u", "state.password"),
        secrets,
        Reachable(),
        required=True,
    )
    source = ConnectivityProbe(target(), secrets, Refused())

    service = HealthService(probes=(state, source))
    report = service.report()

    assert service.readiness() is True
    assert report.ready is True
    by_name = {dependency.name: dependency for dependency in report.dependencies}
    assert by_name["state-store"].state is HealthState.HEALTHY
    assert by_name["aims-portal"].state is HealthState.UNAVAILABLE
    assert by_name["aims-portal"].required is False


def test_a_down_state_store_makes_the_service_unready() -> None:
    secrets = StaticSecrets({"state.password": "y"})
    state = ConnectivityProbe(
        ConnectionTarget("state-store", TargetKind.POSTGRESQL, "h", 5432, "esl", "u", "state.password"),
        secrets,
        Refused(),
        required=True,
    )

    assert HealthService(probes=(state,)).readiness() is False
