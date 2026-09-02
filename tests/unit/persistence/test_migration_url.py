"""Alembic takes the state-store password from the bundle, like the service.

Found in use: migrating the development database required a
password-bearing ESL_DATABASE_URL, which is exactly what the startup gate
refuses for the service since #78. Migrations and the service now resolve
the same credential the same way. A URL that already embeds a password is
still accepted unchanged, because the integration test fixtures and CI
supply the dedicated test database that way and it is not a Settings field.
"""

import pytest

from esl_service.persistence.migration_url import resolve_migration_url
from esl_service.runtime.secrets import SecretUnavailableError


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.reads = 0

    def get(self, name: str) -> str:
        self.reads += 1
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError("requested secret is unavailable") from None


def test_a_password_free_url_gets_the_bundled_state_password() -> None:
    secrets = StaticSecrets({"state.password": "from-bundle"})

    url = resolve_migration_url(
        {"ESL_DATABASE_URL": "postgresql+psycopg://esl_dev@localhost:5432/esl_pipeline_dev"},
        lambda _path: secrets,
    )

    assert url == "postgresql+psycopg://esl_dev:from-bundle@localhost:5432/esl_pipeline_dev"


def test_the_injected_password_is_escaped_for_the_url() -> None:
    """Alembic receives a string, so metacharacters must survive the trip."""

    secrets = StaticSecrets({"state.password": "p@ss:w/rd"})

    url = resolve_migration_url(
        {"ESL_DATABASE_URL": "postgresql+psycopg://u@h:5432/db"}, lambda _path: secrets
    )

    assert "p%40ss%3Aw%2Frd" in url
    assert "p@ss" not in url


def test_a_url_that_embeds_a_password_is_used_unchanged_without_reading_the_bundle() -> None:
    """The test database is not a Settings field and may carry its password."""

    secrets = StaticSecrets({"state.password": "unused"})

    url = resolve_migration_url(
        {"ESL_DATABASE_URL": "postgresql+psycopg://t:inline@localhost:5432/esl_pipeline_test"},
        lambda _path: secrets,
    )

    assert url == "postgresql+psycopg://t:inline@localhost:5432/esl_pipeline_test"
    assert secrets.reads == 0


def test_the_bundle_path_comes_from_the_environment_or_the_default() -> None:
    seen: list[str] = []

    def factory(path: str) -> StaticSecrets:
        seen.append(path)
        return StaticSecrets({"state.password": "x"})

    resolve_migration_url(
        {"ESL_DATABASE_URL": "postgresql+psycopg://u@h/db", "ESL_SECRET_BUNDLE_PATH": r"D:\b.dpapi"},
        factory,
    )
    resolve_migration_url({"ESL_DATABASE_URL": "postgresql+psycopg://u@h/db"}, factory)

    assert seen[0] == r"D:\b.dpapi"
    assert seen[1].endswith("secrets.dpapi")


def test_a_missing_bundle_key_is_a_clear_error_naming_the_key() -> None:
    with pytest.raises(RuntimeError, match="state.password"):
        resolve_migration_url(
            {"ESL_DATABASE_URL": "postgresql+psycopg://u@h/db"},
            lambda _path: StaticSecrets({}),
        )


def test_a_missing_url_is_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="ESL_DATABASE_URL"):
        resolve_migration_url({}, lambda _path: StaticSecrets({}))
