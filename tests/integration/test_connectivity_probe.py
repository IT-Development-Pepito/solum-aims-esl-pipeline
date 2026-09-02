"""The real connector against the dedicated test database (#79).

The unit tests prove classification with synthetic errors. This proves the
real SQLAlchemy connector produces the three outcomes a developer needs to tell
apart -- reachable, wrong password, wrong route -- against an actual PostgreSQL
server, and that a success reports the identity it connected as. It runs only
when the dedicated non-production database is configured, like every other
integration test.
"""

import os

import pytest
from sqlalchemy.engine import make_url

from esl_service.runtime.connectivity import (
    ConnectionTarget,
    ProbeOutcome,
    SqlAlchemyConnector,
    TargetKind,
    probe,
)


class InlineSecrets:
    def __init__(self, value: str) -> None:
        self._value = value

    def get(self, name: str) -> str:
        return self._value


@pytest.fixture
def test_database() -> tuple[str, int, str, str, str]:
    """Split the configured test URL so the probe can be given wrong parts."""

    raw = os.environ.get("ESL_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("ESL_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    url = make_url(raw)
    assert url.host and url.database and url.username and url.password
    return url.host, url.port or 5432, url.database, url.username, url.password


def target(host: str, port: int, database: str, username: str) -> ConnectionTarget:
    return ConnectionTarget(
        name="state-store",
        kind=TargetKind.POSTGRESQL,
        host=host,
        port=port,
        database=database,
        username=username,
        password_key="state.password",
    )


def test_the_real_connector_reaches_the_test_database_and_reports_identity(
    test_database: tuple[str, int, str, str, str],
) -> None:
    host, port, database, username, password = test_database

    result = probe(
        target(host, port, database, username),
        InlineSecrets(password),
        SqlAlchemyConnector(timeout_seconds=5),
    )

    assert result.outcome is ProbeOutcome.REACHABLE
    assert result.identity == username


def test_a_wrong_password_is_reported_as_a_credential_fault(
    test_database: tuple[str, int, str, str, str],
) -> None:
    """The one outcome that cannot be proven without a real server."""

    host, port, database, username, _ = test_database

    result = probe(
        target(host, port, database, username),
        InlineSecrets("definitely-not-the-password"),
        SqlAlchemyConnector(timeout_seconds=5),
    )

    assert result.outcome is ProbeOutcome.CREDENTIAL_REJECTED


def test_a_wrong_port_is_reported_as_unreachable(
    test_database: tuple[str, int, str, str, str],
) -> None:
    host, _, database, username, password = test_database

    result = probe(
        target(host, 1, database, username),
        InlineSecrets(password),
        SqlAlchemyConnector(timeout_seconds=2),
    )

    assert result.outcome is ProbeOutcome.UNREACHABLE
