"""The audit warning names its cause.

Found in use: every `secrets set` printed "the state store is unavailable"
while `check-connections` showed the state store REACHABLE. The store was
fine; its schema had never been migrated, and one broad except turned
"relation audit_entry does not exist" into a message about availability.
A warning that cannot be acted on is not a warning, so the failure is now
classified and each class says what to do.
"""

import base64
from pathlib import Path

import pytest
from typer.testing import CliRunner

from esl_service.runtime import cli
from esl_service.runtime.cli import AuditFailure, classify_audit_failure
from esl_service.runtime.secrets import SecretUnavailableError

runner = CliRunner()


class Base64Codec:
    def protect(self, data: bytes) -> bytes:
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        return base64.b64decode(data)


class NoopProtector:
    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        return None


# --- classification ---------------------------------------------------------


def _wrapped(sqlstate: str) -> Exception:
    class Driver(Exception):
        pass

    inner = Driver("driver text that may embed a url")
    inner.sqlstate = sqlstate  # type: ignore[attr-defined]

    class Wrapper(Exception):
        orig = inner

    return Wrapper()


def test_an_unmigrated_schema_is_its_own_cause() -> None:
    """42P01 undefined_table: the store answered, the tables are not there."""

    assert classify_audit_failure(_wrapped("42P01")) is AuditFailure.SCHEMA_NOT_MIGRATED


def test_a_refused_credential_is_its_own_cause() -> None:
    assert classify_audit_failure(_wrapped("28P01")) is AuditFailure.CREDENTIAL_REJECTED


def test_a_missing_bundle_key_is_its_own_cause() -> None:
    assert classify_audit_failure(SecretUnavailableError("x")) is AuditFailure.SECRET_UNAVAILABLE


def test_a_url_that_embeds_a_password_is_a_configuration_cause() -> None:
    assert classify_audit_failure(ValueError("must not embed a password")) is (
        AuditFailure.CONFIGURATION
    )


def test_anything_else_is_unreachable() -> None:
    assert classify_audit_failure(ConnectionRefusedError()) is AuditFailure.UNREACHABLE


# --- the message says what to do -------------------------------------------


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "_codec", Base64Codec)
    monkeypatch.setattr(cli, "_protector", NoopProtector)
    monkeypatch.setattr(cli, "_current_sid", lambda: "S-1-5-21-1-2-3-1001")
    for name in ("ESL_ENVIRONMENT", "ESL_DATABASE_URL", "ESL_INTERNAL_HOST",
                 "ESL_SERVICE_IDENTITY_SID", "ESL_SECRET_BUNDLE_PATH"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "secrets.dpapi"


def set_secret(bundle: Path) -> str:
    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="v\n",
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(
    ("failure", "expected_phrase"),
    [
        (AuditFailure.SCHEMA_NOT_MIGRATED, "alembic upgrade head"),
        (AuditFailure.CREDENTIAL_REJECTED, "state.password"),
        (AuditFailure.SECRET_UNAVAILABLE, "state.password"),
        (AuditFailure.CONFIGURATION, "ESL_DATABASE_URL"),
        (AuditFailure.UNREACHABLE, "reach"),
        (AuditFailure.NO_SETTINGS, "configuration"),
    ],
)
def test_each_cause_gets_an_actionable_message(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, failure: AuditFailure, expected_phrase: str
) -> None:
    monkeypatch.setattr(cli, "_record_audit", lambda **_: failure)

    output = set_secret(bundle)

    assert "audit" in output.lower()
    assert expected_phrase in output


def test_a_recorded_audit_prints_no_warning(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_record_audit", lambda **_: None)

    output = set_secret(bundle)

    assert "warning" not in output.lower()


def test_the_secret_is_stored_whatever_the_audit_outcome(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning must never be blocked on the thing being provisioned."""

    monkeypatch.setattr(cli, "_record_audit", lambda **_: AuditFailure.SCHEMA_NOT_MIGRATED)

    set_secret(bundle)

    assert bundle.exists()
