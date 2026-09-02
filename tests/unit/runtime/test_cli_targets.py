"""check-connections reports every configured target from settings (#78).

With #78 the CLI no longer needs a --target for the tiers configuration
already names: the state store, warehouse, legacy baseline, PEPITO_HO, and
both AIMS databases. Unconfigured tiers are listed as such, so a gap is
visible while access is still being arranged.
"""

import base64
from pathlib import Path

import pytest
from sqlalchemy.engine import URL
from typer.testing import CliRunner

from esl_service.runtime import cli
from esl_service.runtime.connectivity import ProbeOutcome

runner = CliRunner()


class Base64Codec:
    def protect(self, data: bytes) -> bytes:
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        return base64.b64decode(data)


class NoopProtector:
    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        return None


class Reachable:
    def __init__(self) -> None:
        self.hosts: list[str | None] = []

    def connect_and_identify(self, url: URL) -> str:
        self.hosts.append(url.host)
        return url.username or "?"


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A development-shaped environment with every tier named and no password."""

    monkeypatch.setattr(cli, "_codec", Base64Codec)
    monkeypatch.setattr(cli, "_protector", NoopProtector)
    monkeypatch.setattr(cli, "_current_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(cli, "_record_audit", lambda **_: True)
    env = {
        "ESL_ENVIRONMENT": "development",
        "ESL_DATABASE_URL": "postgresql+psycopg://esl_dev@localhost:5432/esl_pipeline_dev",
        "ESL_INTERNAL_HOST": "127.0.0.1",
        "ESL_SOURCE_SQL_HOST": "sql.internal",
        "ESL_SOURCE_SQL_USERNAME": "esl_reader",
        "ESL_SOURCE_PEPITO_HO_HOST": "ho.internal",
        "ESL_AIMS_HOST": "aims.internal",
        "ESL_AIMS_PORTAL_DATABASE": "AIMS_PORTAL_DB",
        "ESL_AIMS_PORTAL_USERNAME": "esl_aims_reader",
        "ESL_AIMS_CORE_DATABASE": "AIMS_CORE_DB",
        "ESL_AIMS_CORE_USERNAME": "esl_aims_reader",
        "ESL_SERVICE_IDENTITY_SID": "",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    bundle = tmp_path / "secrets.dpapi"
    monkeypatch.setenv("ESL_SECRET_BUNDLE_PATH", str(bundle))
    for key in ("state.password", "source.sql.password", "aims.portal.password", "aims.core.password"):
        runner.invoke(
            cli.app,
            ["secrets", "set", key, "--bundle", str(bundle), "--reason", "r", "--stdin"],
            input="pw\n",
        )
    return bundle


def test_every_configured_tier_is_probed_without_a_target_flag(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = Reachable()
    monkeypatch.setattr(cli, "_connector", lambda: connector)

    result = runner.invoke(cli.app, ["check-connections", "--bundle", str(configured)])

    assert result.exit_code == 0, result.output
    for name in ("state-store", "warehouse", "legacy-baseline", "pepito-ho", "aims-portal", "aims-core"):
        assert name in result.output
    assert result.output.count(ProbeOutcome.REACHABLE.value) == 6
    assert set(connector.hosts) == {"localhost", "sql.internal", "ho.internal", "aims.internal"}


def test_an_unconfigured_tier_is_listed_as_unconfigured_and_does_not_fail(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESL_SOURCE_PEPITO_HO_HOST", "")
    monkeypatch.setattr(cli, "_connector", Reachable)

    result = runner.invoke(cli.app, ["check-connections", "--bundle", str(configured)])

    assert result.exit_code == 0, result.output
    assert "pepito-ho" in result.output
    assert ProbeOutcome.UNCONFIGURED.value in result.output


def test_a_missing_bundle_key_fails_the_check_by_name(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_connector", Reachable)
    runner.invoke(
        cli.app, ["secrets", "remove", "aims.core.password", "--bundle", str(configured), "--reason", "r"]
    )

    result = runner.invoke(cli.app, ["check-connections", "--bundle", str(configured)])

    assert result.exit_code == 1
    line = next(l for l in result.output.splitlines() if l.startswith("aims-core"))
    assert ProbeOutcome.SECRET_UNAVAILABLE.value in line


def test_the_output_never_contains_a_password(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_connector", Reachable)

    result = runner.invoke(cli.app, ["check-connections", "--bundle", str(configured)])

    assert "pw" not in result.output.replace("pw\n", "")
    assert "postgresql://" not in result.output and "mssql" not in result.output
