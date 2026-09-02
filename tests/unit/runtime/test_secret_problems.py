"""Startup names a missing secret by key, never by content (#78, FR-025).

Configuration validation already reports faults by key only. Secrets get the
same treatment: for every configured target whose bundle key cannot be read,
one problem naming the key, with a message that carries no bundle path, no
exception text, and no value.
"""

from esl_service.config import Settings
from esl_service.runtime.secrets import SecretUnavailableError, describe_secret_problems


class StaticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretUnavailableError(
                "requested secret is unavailable at C:\\ProgramData\\SOLUM\\ESL\\secrets.dpapi"
            ) from None


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://u@localhost/esl",
        "internal_host": "127.0.0.1",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_the_state_store_password_is_always_required() -> None:
    problems = describe_secret_problems(settings(), StaticSecrets({}))

    assert [problem.key for problem in problems] == ["secret.state.password"]


def test_a_present_secret_raises_no_problem() -> None:
    problems = describe_secret_problems(settings(), StaticSecrets({"state.password": "x"}))

    assert problems == ()


def test_only_configured_targets_are_checked() -> None:
    """An unconfigured tier has nothing to read; reporting it would be noise."""

    configured = settings(
        aims_host="aims.internal", aims_portal_database="AIMS_PORTAL_DB", aims_portal_username="u"
    )

    keys = [p.key for p in describe_secret_problems(configured, StaticSecrets({"state.password": "x"}))]

    assert keys == ["secret.aims.portal.password"]


def test_one_shared_sql_key_is_reported_once() -> None:
    """Three SQL Server tiers share one credential; one problem, not three."""

    configured = settings(source_sql_host="sql.internal", source_sql_username="r", source_pepito_ho_host="ho")

    keys = [p.key for p in describe_secret_problems(configured, StaticSecrets({"state.password": "x"}))]

    assert keys == ["secret.source.sql.password"]


def test_the_message_discloses_nothing() -> None:
    problems = describe_secret_problems(settings(), StaticSecrets({}))

    message = problems[0].message
    assert "ProgramData" not in message
    assert ".dpapi" not in message
    assert "unavailable" in message
