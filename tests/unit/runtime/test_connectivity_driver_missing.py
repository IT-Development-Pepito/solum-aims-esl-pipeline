"""A missing ODBC driver is its own outcome, and it names the driver.

Found in use: ESL_SOURCE_SQL_DRIVER still held the URL-encoded form
"ODBC+Driver+18+for+SQL+Server", so ODBC looked for a driver literally named
that, answered IM002, and the tool reported UNREACHABLE. The cause was
invisible until the connection was reproduced outside the tool. IM002 is
raised before any network activity, so it is not a route problem and must
not be reported as one.
"""

from sqlalchemy.engine import URL

from esl_service.runtime.connectivity import (
    ConnectionTarget,
    ConnectivityProbe,
    ProbeOutcome,
    TargetKind,
    classify_failure,
    probe,
)
from esl_service.runtime.health import HealthState
from esl_service.runtime.secrets import SecretUnavailableError


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


class OdbcInterfaceError(Exception):
    """Shaped like pyodbc.InterfaceError: SQLSTATE in args[0], text in args[1]."""


class NoDriverConnector:
    """What pyodbc raises when the named ODBC driver is not installed."""

    def connect_and_identify(self, url: URL) -> str:
        raise OdbcInterfaceError(
            "IM002",
            "[IM002] [Microsoft][ODBC Driver Manager] Data source name not found "
            "and no default driver specified (0) (SQLDriverConnect)",
        )


def sql_target(driver: str) -> ConnectionTarget:
    return ConnectionTarget(
        name="warehouse",
        kind=TargetKind.SQLSERVER,
        host="sql.internal",
        port=None,
        database="DBWH_8555",
        username="esl_reader",
        password_key="source.sql.password",
        driver=driver,
    )


def test_im002_is_classified_as_a_missing_driver_not_a_route_fault() -> None:
    assert classify_failure(Exception("IM002", "no default driver")) is ProbeOutcome.DRIVER_MISSING


def test_a_wrapped_im002_is_unwrapped_through_orig() -> None:
    class Inner(Exception):
        pass

    class Wrapper(Exception):
        orig = Inner("IM002", "no default driver")

    assert classify_failure(Wrapper()) is ProbeOutcome.DRIVER_MISSING


def test_the_result_names_the_driver_that_was_looked_for() -> None:
    """The name is the diagnosis: a '+' or a typo becomes visible at once."""

    result = probe(
        sql_target("ODBC+Driver+18+for+SQL+Server"),
        StaticSecrets({"source.sql.password": "x"}),
        NoDriverConnector(),
    )

    assert result.outcome is ProbeOutcome.DRIVER_MISSING
    assert result.ok is False
    assert result.detail is not None
    assert "ODBC+Driver+18+for+SQL+Server" in result.detail
    assert "not installed" in result.detail


def test_the_detail_still_carries_no_connection_string() -> None:
    result = probe(
        sql_target("Nope"), StaticSecrets({"source.sql.password": "needle"}), NoDriverConnector()
    )

    assert "needle" not in (result.detail or "")
    assert "://" not in (result.detail or "")


def test_a_missing_driver_is_unavailable_in_the_health_report() -> None:
    health = ConnectivityProbe(
        sql_target("Nope"), StaticSecrets({"source.sql.password": "x"}), NoDriverConnector()
    ).check()

    assert health.state is HealthState.UNAVAILABLE
    assert health.detail is not None
    assert "Nope" in health.detail
