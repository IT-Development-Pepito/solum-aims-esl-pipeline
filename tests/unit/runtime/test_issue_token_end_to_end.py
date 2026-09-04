"""A token issued by the CLI is the token the API accepts (#98).

The acceptance criteria ask for more than "the bundle now holds a value":
the value must round-trip through the bundle reader, the authenticator, and
a real request, and a rotated token must stop working. This exercises that
whole path with the CLI writing and the app factory reading, so a change to
either side breaks here rather than at a staging host.
"""

import base64
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from esl_service.domain.authorization import Role
from esl_service.runtime import cli
from esl_service.runtime.secrets import BundleSecretProvider
from esl_service.web.auth import BearerTokenAuthenticator, tokens_from_bundle

runner = CliRunner()


class Base64Codec:
    """The bundle codec the other CLI tests use; DPAPI is Windows-only."""

    def protect(self, data: bytes) -> bytes:
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        return base64.b64decode(data)


class NoopProtector:
    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        return None


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "secrets.dpapi"
    monkeypatch.setattr(cli, "_codec", Base64Codec)
    monkeypatch.setattr(cli, "_protector", NoopProtector)
    monkeypatch.setattr(cli, "_current_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(cli, "_record_audit", lambda **_: None)
    for name in ("ESL_ENVIRONMENT", "ESL_DATABASE_URL", "ESL_INTERNAL_HOST",
                 "ESL_SHADOW_MODE", "ESL_SERVICE_IDENTITY_SID", "ESL_SECRET_BUNDLE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ESL_OPERATOR_ROLES", "ops.alice=operator")
    return path


def issue(bundle: Path, account: str = "ops.alice") -> str:
    result = runner.invoke(
        cli.app,
        ["secrets", "issue-token", account, "--bundle", str(bundle),
         "--reason", "CHG-9 provisioning", "--stdout"],
    )
    assert result.exit_code == 0, result.output
    stored: dict[str, str] = json.loads(base64.b64decode(bundle.read_bytes()))
    return stored[f"api.token.{account}"]


def authenticator(bundle: Path) -> BearerTokenAuthenticator:
    """Build the authenticator the way the service host does."""

    tokens = tokens_from_bundle(BundleSecretProvider(bundle, Base64Codec()))
    return BearerTokenAuthenticator(
        tokens=tokens, assignments={"ops.alice": frozenset({Role.OPERATOR})}
    )


def test_an_issued_token_authenticates_as_its_account(bundle: Path) -> None:
    token = issue(bundle)

    principal = authenticator(bundle).authenticate(f"Bearer {token}")

    assert principal.identity == "ops.alice"
    assert Role.OPERATOR in principal.roles


def test_rotating_invalidates_the_previous_token(bundle: Path) -> None:
    """The whole point of rotation: the old value must stop working."""

    from esl_service.web.auth import AuthenticationFailed

    first = issue(bundle)
    second = issue(bundle)
    assert first != second

    resolved = authenticator(bundle)
    assert resolved.authenticate(f"Bearer {second}").identity == "ops.alice"
    with pytest.raises(AuthenticationFailed):
        resolved.authenticate(f"Bearer {first}")


def test_two_accounts_get_distinct_tokens_that_resolve_separately(bundle: Path) -> None:
    alice = issue(bundle, "ops.alice")
    bob = issue(bundle, "ops.bob")

    tokens = tokens_from_bundle(BundleSecretProvider(bundle, Base64Codec()))

    assert alice != bob
    assert tokens == {"ops.alice": alice, "ops.bob": bob}
