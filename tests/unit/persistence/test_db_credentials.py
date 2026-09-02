"""The state store engine takes its password from the bundle (#78, AD-017).

ESL_DATABASE_URL names where the state store is and as whom to connect. The
password is injected from the bundle at engine creation, so it never sits in
the environment and never appears in a rendered URL. Creating an engine does
not open a connection, so these tests need no database.
"""

import pytest

from esl_service.config import Settings
from esl_service.persistence.db import create_database_engine_from_settings
from esl_service.runtime.secrets import SecretUnavailableError


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


def settings(url: str = "postgresql+psycopg://esl_dev@localhost:5432/esl") -> Settings:
    return Settings.model_validate(
        {"environment": "development", "database_url": url, "internal_host": "127.0.0.1"}
    )


def test_the_password_is_taken_from_the_bundle() -> None:
    engine = create_database_engine_from_settings(
        settings(), StaticSecrets({"state.password": "from-bundle"})
    )

    assert engine.url.password == "from-bundle"
    assert engine.url.username == "esl_dev"
    assert engine.url.database == "esl"


def test_the_rendered_url_never_shows_the_password() -> None:
    engine = create_database_engine_from_settings(
        settings(), StaticSecrets({"state.password": "needle-x1"})
    )

    assert "needle-x1" not in str(engine.url)
    assert "needle-x1" not in repr(engine.url)


def test_a_url_that_already_embeds_a_password_is_refused() -> None:
    """Two sources of truth for one credential is how a rotation gets missed."""

    with pytest.raises(ValueError, match="password"):
        create_database_engine_from_settings(
            settings("postgresql+psycopg://esl_dev:inline@localhost/esl"),
            StaticSecrets({"state.password": "x"}),
        )


def test_a_missing_bundle_key_surfaces_as_the_non_disclosing_error() -> None:
    with pytest.raises(SecretUnavailableError):
        create_database_engine_from_settings(settings(), StaticSecrets({}))


def test_a_password_with_url_metacharacters_survives() -> None:
    """Injection happens on the parsed URL, so nothing needs escaping."""

    engine = create_database_engine_from_settings(
        settings(), StaticSecrets({"state.password": "p@ss:w/rd#1?x=y"})
    )

    assert engine.url.password == "p@ss:w/rd#1?x=y"
