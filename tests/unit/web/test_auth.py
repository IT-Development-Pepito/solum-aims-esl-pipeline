"""Bearer-token authentication for the internal operations API (FR-029, AD-019).

The owner decided on 2026-09-02: each account that may call the API holds a
token provisioned into the DPAPI bundle under ``api.token.<account>``. A
request presenting a token is the named account; its roles come from
``ESL_OPERATOR_ROLES`` exactly as for the CLI (AD-018), so both surfaces
share one authorization model. Tokens are compared in constant time and are
never logged, echoed, or included in an error.
"""

import pytest

from esl_service.domain.authorization import Role
from esl_service.runtime.secrets import SecretUnavailableError
from esl_service.web.auth import (
    API_TOKEN_PREFIX,
    AuthenticationFailed,
    BearerTokenAuthenticator,
    tokens_from_bundle,
)


class FakeProvider:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


ASSIGNMENTS = {"pepito": frozenset({Role.ADMIN}), "budi": frozenset({Role.OPERATOR})}


@pytest.fixture
def authenticator() -> BearerTokenAuthenticator:
    return BearerTokenAuthenticator(
        tokens={"Pepito": "tok-admin-needle", "budi": "tok-op-needle"},
        assignments=ASSIGNMENTS,
    )


def test_a_valid_token_names_the_account_with_its_configured_roles(
    authenticator: BearerTokenAuthenticator,
) -> None:
    principal = authenticator.authenticate("Bearer tok-op-needle")

    assert principal.identity == "budi"
    assert principal.roles == frozenset({Role.OPERATOR})


def test_the_account_name_is_matched_to_roles_case_insensitively(
    authenticator: BearerTokenAuthenticator,
) -> None:
    principal = authenticator.authenticate("Bearer tok-admin-needle")

    assert principal.identity == "Pepito"
    assert principal.roles == frozenset({Role.ADMIN})


def test_an_account_with_a_token_but_no_role_is_authenticated_but_powerless() -> None:
    """Authentication and authorization stay separate: the refusal is audited later."""

    authenticator = BearerTokenAuthenticator(tokens={"guest": "tok-guest"}, assignments={})

    principal = authenticator.authenticate("Bearer tok-guest")

    assert principal.identity == "guest"
    assert principal.roles == frozenset()


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic tok-op-needle", "Bearer wrong", "tok-op-needle"],
)
def test_a_missing_malformed_or_unknown_token_is_refused(
    authenticator: BearerTokenAuthenticator, header: str | None
) -> None:
    with pytest.raises(AuthenticationFailed):
        authenticator.authenticate(header)


def test_the_refusal_never_echoes_the_presented_or_stored_token(
    authenticator: BearerTokenAuthenticator,
) -> None:
    with pytest.raises(AuthenticationFailed) as caught:
        authenticator.authenticate("Bearer presented-needle")

    text = str(caught.value)
    assert "presented-needle" not in text
    assert "needle" not in text


def test_an_empty_token_can_never_match() -> None:
    """An account provisioned with an empty value must not become a wildcard."""

    authenticator = BearerTokenAuthenticator(tokens={"x": ""}, assignments={})

    with pytest.raises(AuthenticationFailed):
        authenticator.authenticate("Bearer ")


# --- tokens come from the bundle ---------------------------------------------


def test_tokens_are_read_from_the_bundle_by_prefix() -> None:
    provider = FakeProvider(
        {
            "state.password": "pw-needle",
            f"{API_TOKEN_PREFIX}Pepito": "tok-a",
            f"{API_TOKEN_PREFIX}budi": "tok-b",
        }
    )

    tokens = tokens_from_bundle(provider)

    assert tokens == {"Pepito": "tok-a", "budi": "tok-b"}
    assert "pw-needle" not in tokens.values()


def test_a_bundle_without_tokens_yields_no_accounts() -> None:
    assert tokens_from_bundle(FakeProvider({"state.password": "pw"})) == {}


def test_an_unreadable_bundle_is_reported_as_unavailable_not_as_empty() -> None:
    class Broken:
        def names(self) -> tuple[str, ...]:
            raise SecretUnavailableError("secret bundle is unavailable")

        def get(self, name: str) -> str:
            raise SecretUnavailableError("secret bundle is unavailable")

    with pytest.raises(SecretUnavailableError):
        tokens_from_bundle(Broken())
