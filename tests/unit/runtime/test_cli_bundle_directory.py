"""A missing bundle directory, and a filesystem refusal, are handled, not dumped.

Found in use: the first `esl-admin secrets set` on a machine without
C:\\ProgramData\\SOLUM\\ESL ended in a raw FileNotFoundError traceback. The
tool promised controlled, non-disclosing failures, and a PermissionError,
which is what a wrong production ACL produces, would have escaped the same
way.

The directory is never created silently in staging or production: the
startup validator checks the directory's owner and ACL, so a folder created
with inherited permissions would be accepted here and rejected by the
service. On a development machine, where no service identity is configured,
creating it is the helpful thing to do, and the tool says it did.
"""

import base64
from pathlib import Path

import pytest
from typer.testing import CliRunner

from esl_service.runtime import cli

runner = CliRunner()
SID = "S-1-5-21-1-2-3-1001"


class Base64Codec:
    def protect(self, data: bytes) -> bytes:
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        return base64.b64decode(data)


class NoopProtector:
    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        return None


class DeniedProtector:
    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        raise PermissionError(13, "Access is denied", str(path))


@pytest.fixture
def missing_dir_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bundle path whose directory does not exist yet."""

    monkeypatch.setattr(cli, "_codec", Base64Codec)
    monkeypatch.setattr(cli, "_protector", NoopProtector)
    monkeypatch.setattr(cli, "_current_sid", lambda: SID)
    monkeypatch.setattr(cli, "_record_audit", lambda **_: True)
    for name in ("ESL_ENVIRONMENT", "ESL_DATABASE_URL", "ESL_INTERNAL_HOST",
                 "ESL_SERVICE_IDENTITY_SID", "ESL_SECRET_BUNDLE_PATH"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "ProgramData" / "SOLUM" / "ESL" / "secrets.dpapi"


def set_secret(bundle: Path) -> "cli.typer.testing.Result":  # type: ignore[name-defined]
    return runner.invoke(
        cli.app,
        ["secrets", "set", "state.password", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="value\n",
    )


# --- development: create the directory and say so ------------------------


def test_on_a_development_machine_the_directory_is_created_and_announced(
    missing_dir_bundle: Path,
) -> None:
    result = set_secret(missing_dir_bundle)

    assert result.exit_code == 0, result.output
    assert missing_dir_bundle.parent.is_dir()
    assert missing_dir_bundle.exists()
    assert "created" in result.output.lower()
    assert str(missing_dir_bundle.parent) in result.output


# --- staging and production: refuse, and never create it silently ---------


def test_with_a_service_identity_a_missing_directory_is_refused_not_created(
    missing_dir_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator checks the directory's ACL; a folder made here would fail it."""

    monkeypatch.setenv("ESL_SERVICE_IDENTITY_SID", SID)

    result = set_secret(missing_dir_bundle)

    assert result.exit_code == 1
    assert not missing_dir_bundle.parent.exists()
    assert "directory" in result.output.lower()
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, OSError)


def test_remove_on_a_missing_directory_is_refused_the_same_way(
    missing_dir_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESL_SERVICE_IDENTITY_SID", SID)

    result = runner.invoke(
        cli.app, ["secrets", "remove", "k", "--bundle", str(missing_dir_bundle), "--reason", "r"]
    )

    assert result.exit_code == 1
    assert not missing_dir_bundle.parent.exists()
    assert "Traceback" not in result.output


# --- a filesystem refusal is reported, not raised --------------------------


def test_a_permission_error_becomes_a_controlled_failure(
    missing_dir_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a wrong production ACL produces must not escape as a traceback."""

    missing_dir_bundle.parent.mkdir(parents=True)
    monkeypatch.setattr(cli, "_protector", DeniedProtector)

    result = set_secret(missing_dir_bundle)

    assert result.exit_code == 1
    assert "permission" in result.output.lower()
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, OSError)
    assert not list(missing_dir_bundle.parent.glob("*.tmp")), "no temporary file may remain"
    assert not missing_dir_bundle.exists()
