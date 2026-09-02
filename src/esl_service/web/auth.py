"""Bearer-token authentication for the internal operations API (FR-029, AD-019).

The owner decided on 2026-09-02: each account that may call the API holds a
token provisioned into the DPAPI bundle under ``api.token.<account>`` with
``esl-admin secrets set``. A request presenting that token *is* the account;
its roles come from ``ESL_OPERATOR_ROLES`` exactly as for the CLI (AD-018),
so both surfaces share one authorization model and one audit vocabulary.

Authentication and authorization stay separate on purpose. An account with a
token but no role is authenticated and then refused by the #26 service, which
audits the refusal under that account's name; an unknown token is refused
here as 401 and is never written anywhere, because the presented value might
be a credential for something else.
"""

import hmac
from collections.abc import Mapping
from typing import Protocol

from esl_service.domain.authorization import Principal, Role, principal_for

#: Bundle keys ``api.token.<account>`` hold API tokens; the suffix is the account.
API_TOKEN_PREFIX = "api.token."
_SCHEME = "bearer"


class AuthenticationFailed(Exception):
    """Raised when no token, a malformed header, or an unknown token is presented."""

    def __init__(self) -> None:
        super().__init__("a valid bearer token is required")


class TokenSource(Protocol):
    """A bundle reader that can list names and fetch one value."""

    def names(self) -> tuple[str, ...]: ...

    def get(self, name: str) -> str: ...


def tokens_from_bundle(provider: TokenSource) -> dict[str, str]:
    """Return ``{account: token}`` for every ``api.token.*`` key in the bundle.

    An unreadable bundle propagates ``SecretUnavailableError`` rather than
    yielding an empty mapping: "nobody can authenticate" and "the bundle is
    broken" must stay distinguishable.
    """

    return {
        name[len(API_TOKEN_PREFIX) :]: provider.get(name)
        for name in provider.names()
        if name.startswith(API_TOKEN_PREFIX) and len(name) > len(API_TOKEN_PREFIX)
    }


class BearerTokenAuthenticator:
    """Resolves an ``Authorization`` header to a principal, or refuses."""

    def __init__(
        self, tokens: Mapping[str, str], assignments: Mapping[str, frozenset[Role]]
    ) -> None:
        self._tokens = {account: token for account, token in tokens.items() if token}
        self._assignments = dict(assignments)

    def authenticate(self, authorization: str | None) -> Principal:
        """Return the principal the header proves, or raise ``AuthenticationFailed``."""

        presented = _bearer_value(authorization)
        if presented is None:
            raise AuthenticationFailed()

        matched: str | None = None
        presented_bytes = presented.encode("utf-8")
        # Compare against every token so timing does not reveal which
        # accounts exist; ``compare_digest`` keeps each comparison constant.
        for account, token in self._tokens.items():
            if hmac.compare_digest(presented_bytes, token.encode("utf-8")):
                matched = account
        if matched is None:
            raise AuthenticationFailed()
        return principal_for(matched, self._assignments)


def _bearer_value(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != _SCHEME or not value.strip():
        return None
    return value.strip()
