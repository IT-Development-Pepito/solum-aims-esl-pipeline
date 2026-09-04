"""The administrative CLI (#79).

Two commands, both usable without the service running: ``secrets`` to write
the DPAPI bundle, and ``check-connections`` to prove a credential works. The
tests drive the real Typer application through its runner and replace only the
Windows-specific edges -- the codec, the file protector, the process identity,
and the database connector -- with fakes injected through the module's factory
hooks. Nothing here touches a real bundle, a real ACL, or a real database.
"""

import base64
import json
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


class FakeConnector:
    def __init__(self, identity: str = "esl_reader", error: Exception | None = None) -> None:
        self.identity = identity
        self.error = error

    def connect_and_identify(self, url: URL) -> str:
        if self.error is not None:
            raise self.error
        return self.identity


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary bundle with test-only edges."""

    path = tmp_path / "secrets.dpapi"
    monkeypatch.setattr(cli, "_codec", Base64Codec)
    monkeypatch.setattr(cli, "_protector", NoopProtector)
    monkeypatch.setattr(cli, "_current_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(cli, "_record_audit", lambda **_: None)
    for name in ("ESL_ENVIRONMENT", "ESL_DATABASE_URL", "ESL_INTERNAL_HOST",
                 "ESL_SHADOW_MODE", "ESL_SERVICE_IDENTITY_SID", "ESL_SECRET_BUNDLE_PATH"):
        monkeypatch.delenv(name, raising=False)
    return path


def decoded(path: Path) -> dict[str, str]:
    data: dict[str, str] = json.loads(base64.b64decode(path.read_bytes()))
    return data


# --- secrets set ------------------------------------------------------------


def test_set_reads_the_value_from_stdin_and_never_echoes_it(bundle: Path) -> None:
    """The value must not appear in output, so it cannot reach a log or scrollback."""

    result = runner.invoke(
        cli.app,
        ["secrets", "set", "aims.portal.password", "--bundle", str(bundle),
         "--reason", "CHG-1 provisioning", "--stdin"],
        input="needle-value\n",
    )

    assert result.exit_code == 0, result.output
    assert "needle-value" not in result.output
    assert decoded(bundle) == {"aims.portal.password": "needle-value"}


def test_set_prompts_with_hidden_input_when_stdin_is_not_requested(bundle: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r"],
        input="hidden-value\nhidden-value\n",
    )

    assert result.exit_code == 0, result.output
    assert "hidden-value" not in result.output
    assert decoded(bundle) == {"k": "hidden-value"}


def test_set_requires_a_reason(bundle: Path) -> None:
    result = runner.invoke(
        cli.app, ["secrets", "set", "k", "--bundle", str(bundle), "--stdin"], input="v\n"
    )

    assert result.exit_code != 0


def test_set_reports_the_name_and_never_the_value(bundle: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="v-needle\n",
    )

    assert "k" in result.output
    assert "v-needle" not in result.output


# --- identity guard (acceptance criterion) -----------------------------------


def test_set_is_refused_when_running_as_the_wrong_account(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under user scope the wrong writer produces an unreadable bundle later."""

    monkeypatch.setenv("ESL_SERVICE_IDENTITY_SID", "S-1-5-21-9-9-9-9999")

    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="v\n",
    )

    assert result.exit_code == 2
    assert "service account" in result.output
    assert not bundle.exists()


def test_set_warns_but_proceeds_when_no_service_identity_is_configured(bundle: Path) -> None:
    """A development machine has no service account; say so, do not block."""

    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="v\n",
    )

    assert result.exit_code == 0, result.output
    assert "identity check skipped" in result.output.lower()


# --- secrets list / remove ----------------------------------------------------


def test_list_shows_names_only(bundle: Path) -> None:
    runner.invoke(
        cli.app,
        ["secrets", "set", "alpha", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="alpha-needle\n",
    )

    result = runner.invoke(cli.app, ["secrets", "list", "--bundle", str(bundle)])

    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "alpha-needle" not in result.output


def test_list_reports_an_unreadable_bundle_without_disclosing_why(bundle: Path) -> None:
    bundle.write_bytes(b"\xff\xfe not a bundle")

    result = runner.invoke(cli.app, ["secrets", "list", "--bundle", str(bundle)])

    assert result.exit_code != 0
    assert "unavailable" in result.output.lower()
    assert "\\xff" not in result.output and "not a bundle" not in result.output


def test_remove_deletes_one_name(bundle: Path) -> None:
    for name in ("a", "b"):
        runner.invoke(
            cli.app,
            ["secrets", "set", name, "--bundle", str(bundle), "--reason", "r", "--stdin"],
            input="v\n",
        )

    result = runner.invoke(
        cli.app, ["secrets", "remove", "a", "--bundle", str(bundle), "--reason", "r"]
    )

    assert result.exit_code == 0, result.output
    assert decoded(bundle) == {"b": "v"}


# --- audit is recorded, best effort ---------------------------------------


def test_set_records_an_audit_entry_naming_actor_key_and_action(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, object]] = []

    def capture(**fields: object) -> None:
        recorded.append(fields)

    monkeypatch.setattr(cli, "_record_audit", capture)

    runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "CHG-7", "--stdin"],
        input="v-needle\n",
    )

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["action"] == "secret.set"
    assert entry["resource_key"] == "k"
    assert entry["reason"] == "CHG-7"
    assert "v-needle" not in json.dumps({k: str(v) for k, v in entry.items()})


def test_set_still_succeeds_when_audit_cannot_be_recorded(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning may happen before the state store is reachable at all."""

    monkeypatch.setattr(cli, "_record_audit", lambda **_: cli.AuditFailure.UNREACHABLE)

    result = runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="v\n",
    )

    assert result.exit_code == 0, result.output
    assert "audit" in result.output.lower()
    assert decoded(bundle) == {"k": "v"}


# --- check-connections ---------------------------------------------------------


def test_check_connections_reports_each_target_and_exits_zero_when_all_reach(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_connector", lambda: FakeConnector("esl_aims_reader"))
    runner.invoke(
        cli.app,
        ["secrets", "set", "aims.portal.password", "--bundle", str(bundle),
         "--reason", "r", "--stdin"],
        input="pw\n",
    )

    result = runner.invoke(
        cli.app,
        ["check-connections", "--bundle", str(bundle),
         "--target", "aims-portal=postgresql://esl_aims_reader@localhost:5432/AIMS_PORTAL_DB#aims.portal.password"],
    )

    assert result.exit_code == 0, result.output
    assert "aims-portal" in result.output
    assert ProbeOutcome.REACHABLE.value in result.output
    assert "esl_aims_reader" in result.output
    assert "pw" not in result.output.split("esl_aims_reader")[-1]


def test_check_connections_exits_nonzero_when_any_target_fails(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "_connector", lambda: FakeConnector(error=ConnectionRefusedError())
    )
    runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="pw\n",
    )

    result = runner.invoke(
        cli.app,
        ["check-connections", "--bundle", str(bundle),
         "--target", "t=postgresql://u@h:5432/db#k"],
    )

    assert result.exit_code == 1
    assert ProbeOutcome.UNREACHABLE.value in result.output


def test_check_connections_distinguishes_a_missing_secret(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_connector", lambda: FakeConnector())

    result = runner.invoke(
        cli.app,
        ["check-connections", "--bundle", str(bundle),
         "--target", "t=postgresql://u@h:5432/db#never.set"],
    )

    assert result.exit_code == 1
    assert ProbeOutcome.SECRET_UNAVAILABLE.value in result.output


def test_check_connections_with_nothing_configured_is_not_a_failure(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Useful while access is still being arranged."""

    monkeypatch.setattr(cli, "_connector", lambda: FakeConnector())

    result = runner.invoke(cli.app, ["check-connections", "--bundle", str(bundle)])

    assert result.exit_code == 0, result.output
    assert ProbeOutcome.UNCONFIGURED.value in result.output


def test_check_connections_never_prints_a_connection_string(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "_connector",
        lambda: FakeConnector(error=RuntimeError("postgresql://u:pw@h/db refused")),
    )
    runner.invoke(
        cli.app,
        ["secrets", "set", "k", "--bundle", str(bundle), "--reason", "r", "--stdin"],
        input="pw\n",
    )

    result = runner.invoke(
        cli.app,
        ["check-connections", "--bundle", str(bundle),
         "--target", "t=postgresql://u@h:5432/db#k"],
    )

    assert "postgresql://" not in result.output
    assert ":pw@" not in result.output


# --- secrets issue-token (#98) ------------------------------------------------


def issue(bundle: Path, account: str = "ops.alice", *extra: str) -> object:
    return runner.invoke(
        cli.app,
        ["secrets", "issue-token", account, "--bundle", str(bundle),
         "--reason", "CHG-9 provisioning", "--stdout", *extra],
    )


def test_issue_token_stores_it_under_the_account_and_reveals_it_once(bundle: Path) -> None:
    """The one reveal channel is stdout here; the bundle holds the same value."""

    result = issue(bundle)

    assert result.exit_code == 0, result.output
    stored = decoded(bundle)
    assert list(stored) == ["api.token.ops.alice"]
    token = stored["api.token.ops.alice"]
    assert len(token) >= 43  # 32 random bytes, url-safe base64
    assert token in result.output


def test_a_reissued_token_replaces_the_previous_one(bundle: Path) -> None:
    issue(bundle)
    first = decoded(bundle)["api.token.ops.alice"]

    result = issue(bundle)

    second = decoded(bundle)["api.token.ops.alice"]
    assert result.exit_code == 0, result.output
    assert second != first
    assert first not in result.output


def test_a_rotated_token_is_audited_as_a_rotation_and_never_by_value(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_record_audit", lambda **fields: entries.append(fields) or None)

    issue(bundle)
    first_token = decoded(bundle)["api.token.ops.alice"]
    issue(bundle)
    second_token = decoded(bundle)["api.token.ops.alice"]

    assert [entry["action"] for entry in entries] == ["secret.set", "secret.set"]
    assert all(entry["resource_key"] == "api.token.ops.alice" for entry in entries)
    rendered = repr(entries)
    assert first_token not in rendered and second_token not in rendered


def test_issue_token_writes_a_protected_file_and_refuses_to_overwrite(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected: list[Path] = []

    class RecordingProtector:
        def protect(self, path: Path, service_identity_sid: str | None) -> None:
            protected.append(path)

    monkeypatch.setattr(cli, "_protector", RecordingProtector)
    out = tmp_path / "tokens" / "ops.alice.token"

    result = runner.invoke(
        cli.app,
        ["secrets", "issue-token", "ops.alice", "--bundle", str(bundle),
         "--reason", "CHG-9", "--out", str(out)],
    )

    assert result.exit_code == 0, result.output
    token = decoded(bundle)["api.token.ops.alice"]
    assert out.read_text(encoding="utf-8").strip() == token
    assert out in protected  # the ACL was applied to the reveal file
    assert token not in result.output  # --out is the only reveal channel

    again = runner.invoke(
        cli.app,
        ["secrets", "issue-token", "ops.alice", "--bundle", str(bundle),
         "--reason", "CHG-9", "--out", str(out)],
    )

    assert again.exit_code == 1
    assert "exists" in again.output


def test_issue_token_needs_one_reveal_channel(bundle: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["secrets", "issue-token", "ops.alice", "--bundle", str(bundle), "--reason", "r"],
    )

    assert result.exit_code == 2
    assert "--stdout" in result.output and "--out" in result.output


def test_issue_token_refuses_an_account_name_that_is_not_a_bundle_key(bundle: Path) -> None:
    result = issue(bundle, "ops alice!")

    assert result.exit_code == 1
    assert "Refused" in result.output
    assert not bundle.exists()


def test_issue_token_warns_when_the_account_has_no_role(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token that authenticates and is then refused is a support call, not a setup."""

    monkeypatch.setenv("ESL_OPERATOR_ROLES", "ops.bob=operator")

    result = issue(bundle)

    assert result.exit_code == 0, result.output
    assert "ESL_OPERATOR_ROLES" in result.output
    assert "ops.alice" in result.output


def test_issue_token_is_refused_when_running_as_the_wrong_account(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESL_SERVICE_IDENTITY_SID", "S-1-5-21-9-9-9-9999")
    monkeypatch.setenv("ESL_ENVIRONMENT", "production")
    monkeypatch.setenv("ESL_DATABASE_URL", "postgresql+psycopg://esl@db:5432/esl")
    monkeypatch.setenv("ESL_INTERNAL_HOST", "127.0.0.1")

    result = issue(bundle)

    assert result.exit_code == 2
    assert not bundle.exists()
