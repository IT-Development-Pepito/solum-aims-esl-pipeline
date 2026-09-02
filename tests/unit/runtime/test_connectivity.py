"""On-demand connectivity checks (#79).

A connectivity check answers one question per target -- can we reach it, and
as whom -- and never prints how it tried. The outcomes are deliberately
distinct so a developer can tell a credential fault from a route fault from a
missing secret from a target nobody has configured yet.
"""

import pytest
from sqlalchemy.engine import URL

from esl_service.runtime.connectivity import (
    ConnectionTarget,
    ProbeOutcome,
    TargetKind,
    classify_failure,
    parse_target,
    probe,
)
from esl_service.runtime.secrets import SecretUnavailableError


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


class ReachableConnector:
    def __init__(self, identity: str = "esl_reader") -> None:
        self.identity = identity
        self.urls: list[URL] = []

    def connect_and_identify(self, url: URL) -> str:
        self.urls.append(url)
        return self.identity


class FailingConnector:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def connect_and_identify(self, url: URL) -> str:
        raise self.error


def target(**overrides: object) -> ConnectionTarget:
    values: dict[str, object] = {
        "name": "aims-portal",
        "kind": TargetKind.POSTGRESQL,
        "host": "localhost",
        "port": 5432,
        "database": "AIMS_PORTAL_DB",
        "username": "esl_aims_reader",
        "password_key": "aims.portal.password",
    }
    values.update(overrides)
    return ConnectionTarget(**values)  # type: ignore[arg-type]


# --- the URL is built safely and never renders its password ---------------


def test_the_url_is_built_from_parts_not_concatenated() -> None:
    """A password with URL metacharacters must survive untouched."""

    url = target().sqlalchemy_url("p@ss:w/rd#1")

    assert url.password == "p@ss:w/rd#1"
    assert url.host == "localhost"
    assert url.database == "AIMS_PORTAL_DB"
    assert url.drivername.startswith("postgresql")


def test_rendering_the_url_masks_the_password() -> None:
    url = target().sqlalchemy_url("needle-secret")

    assert "needle-secret" not in str(url)
    assert "needle-secret" not in repr(url)


def test_a_sql_server_target_uses_the_pyodbc_driver_name() -> None:
    url = target(kind=TargetKind.SQLSERVER, port=None).sqlalchemy_url("x")

    assert url.drivername == "mssql+pyodbc"
    assert url.port is None


# --- outcomes are distinct (acceptance criterion) ---------------------------


def test_a_reachable_target_reports_the_connected_identity() -> None:
    result = probe(target(), StaticSecrets({"aims.portal.password": "x"}), ReachableConnector())

    assert result.outcome is ProbeOutcome.REACHABLE
    assert result.identity == "esl_reader"


def test_a_missing_secret_is_its_own_outcome() -> None:
    """Nothing was even attempted, and the report must say so."""

    connector = ReachableConnector()
    result = probe(target(), StaticSecrets({}), connector)

    assert result.outcome is ProbeOutcome.SECRET_UNAVAILABLE
    assert connector.urls == [], "no connection may be attempted without a secret"


def test_a_rejected_credential_is_distinguished_from_an_unreachable_host() -> None:
    class PgAuthError(Exception):
        sqlstate = "28P01"

    rejected = probe(
        target(), StaticSecrets({"aims.portal.password": "wrong"}),
        FailingConnector(PgAuthError("password authentication failed")),
    )
    unreachable = probe(
        target(), StaticSecrets({"aims.portal.password": "x"}),
        FailingConnector(ConnectionRefusedError("connection refused")),
    )

    assert rejected.outcome is ProbeOutcome.CREDENTIAL_REJECTED
    assert unreachable.outcome is ProbeOutcome.UNREACHABLE


def test_an_unconfigured_target_is_reported_not_failed() -> None:
    """Useful while access is still being arranged."""

    result = probe(target(host=""), StaticSecrets({}), ReachableConnector())

    assert result.outcome is ProbeOutcome.UNCONFIGURED


def test_a_failure_detail_never_carries_the_exception_text() -> None:
    """Driver messages commonly embed the connection string."""

    result = probe(
        target(), StaticSecrets({"aims.portal.password": "needle"}),
        FailingConnector(RuntimeError("postgresql://u:needle@h/db refused")),
    )

    assert "needle" not in (result.detail or "")
    assert "postgresql://" not in (result.detail or "")


# --- classification handles both drivers' error shapes --------------------


def test_psycopg_style_sqlstate_28p01_is_a_credential_fault() -> None:
    class E(Exception):
        sqlstate = "28P01"

    assert classify_failure(E()) is ProbeOutcome.CREDENTIAL_REJECTED


def test_pyodbc_style_sqlstate_28000_is_a_credential_fault() -> None:
    """pyodbc puts the SQLSTATE in args[0]; SQL Server login failure is 28000."""

    assert classify_failure(Exception("28000", "Login failed for user")) is (
        ProbeOutcome.CREDENTIAL_REJECTED
    )


def test_a_wrapped_driver_error_is_unwrapped_through_orig() -> None:
    """SQLAlchemy wraps the DBAPI error and exposes it as ``.orig``."""

    class Inner(Exception):
        sqlstate = "28P01"

    class Wrapper(Exception):
        orig = Inner()

    assert classify_failure(Wrapper()) is ProbeOutcome.CREDENTIAL_REJECTED


def test_anything_else_is_unreachable() -> None:
    assert classify_failure(TimeoutError()) is ProbeOutcome.UNREACHABLE


# --- targets can be given on the command line -----------------------------


def test_a_target_argument_parses_into_a_target() -> None:
    parsed = parse_target(
        "aims-portal=postgresql://esl_aims_reader@localhost:5432/AIMS_PORTAL_DB#aims.portal.password"
    )

    assert parsed == target()


def test_a_target_argument_without_a_password_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="password key"):
        parse_target("x=postgresql://u@h:5432/db")


def test_a_target_argument_with_an_inline_password_is_rejected() -> None:
    """The whole point is that passwords never appear on a command line."""

    with pytest.raises(ValueError, match="password"):
        parse_target("x=postgresql://u:pw@h:5432/db#key")


def test_an_unknown_target_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="kind"):
        parse_target("x=mysql://u@h/db#key")
