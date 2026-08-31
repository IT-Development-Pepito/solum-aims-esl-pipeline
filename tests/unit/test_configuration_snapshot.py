"""Versioned, secret-free configuration validation and snapshots (FR-025).

Configuration is externalised from business logic and validated at startup.
A validation failure identifies the offending key and never its value, because
pydantic's own error payload carries the rejected input, which for a database
URL is a credential.
"""

import os

import pytest

from esl_service.config import (
    CONFIGURATION_SCHEMA_VERSION,
    Settings,
    configuration_content_hash,
    describe_configuration_problems,
    sanitized_configuration_snapshot,
    validate_startup_configuration,
)

SECRET_URL = "postgresql+psycopg://esl_user:sup3rs3cret@db.internal:5432/esl"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ESL_ variables so these unit tests are deterministic.

    Settings intentionally reads ESL_-prefixed environment variables, so a
    developer or CI shell that exports one would otherwise supply a key this
    module asserts is missing.
    """

    for name in [key for key in os.environ if key.startswith("ESL_")]:
        monkeypatch.delenv(name, raising=False)


def valid_values(**overrides: object) -> dict[str, object]:
    """Build a valid development settings mapping."""

    values: dict[str, object] = {
        "environment": "development",
        "database_url": SECRET_URL,
        "internal_host": "127.0.0.1",
        "shadow_mode": True,
    }
    values.update(overrides)
    return values


# --- startup validation names keys, never values ----------------------------


def test_valid_configuration_reports_no_problem() -> None:
    """A complete development configuration validates cleanly."""

    settings, problems = validate_startup_configuration(valid_values())
    assert problems == ()
    assert settings is not None
    assert settings.environment == "development"


def test_missing_key_is_reported_by_name() -> None:
    """A missing required key is identified so an operator can fix it."""

    values = valid_values()
    del values["database_url"]
    settings, problems = validate_startup_configuration(values)

    assert settings is None
    assert any(problem.key == "database_url" for problem in problems)


def test_validation_problem_never_carries_the_rejected_value() -> None:
    """Pydantic's error payload includes the input; it must not be surfaced."""

    _, problems = validate_startup_configuration(
        valid_values(environment="not-an-environment", database_url=SECRET_URL)
    )
    rendered = " ".join(f"{problem.key} {problem.message}" for problem in problems)

    assert "not-an-environment" not in rendered
    assert "sup3rs3cret" not in rendered
    assert "environment" in rendered


def test_unknown_key_is_reported_rather_than_ignored() -> None:
    """An unrecognised key is a configuration error, not silently dropped."""

    _, problems = validate_startup_configuration(valid_values(unexpected_key="x"))
    assert any(problem.key == "unexpected_key" for problem in problems)


def test_describe_problems_is_stable_and_sorted() -> None:
    """Problems are reported deterministically for operator comparison."""

    values = valid_values()
    del values["database_url"]
    del values["internal_host"]
    _, problems = validate_startup_configuration(values)

    assert [problem.key for problem in problems] == sorted(
        problem.key for problem in problems
    )


def test_describe_configuration_problems_accepts_a_validation_error() -> None:
    """The sanitiser is usable directly on a pydantic error."""

    from pydantic import ValidationError

    try:
        Settings.model_validate(valid_values(environment="bad"))
    except ValidationError as error:
        problems = describe_configuration_problems(error)
    assert any(problem.key == "environment" for problem in problems)


# --- sanitized, versioned snapshot ------------------------------------------


def test_snapshot_excludes_secret_bearing_settings() -> None:
    """A configuration version is secret-free (architecture 5.4)."""

    settings = Settings.model_validate(valid_values())
    snapshot = sanitized_configuration_snapshot(settings)

    assert "database_url" not in snapshot
    assert "sup3rs3cret" not in str(snapshot)
    assert snapshot["environment"] == "development"
    assert snapshot["shadow_mode"] is True


def test_snapshot_carries_its_schema_version() -> None:
    """The snapshot is versioned so a later shape change stays comparable."""

    settings = Settings.model_validate(valid_values())
    assert (
        sanitized_configuration_snapshot(settings)["configuration_schema_version"]
        == CONFIGURATION_SCHEMA_VERSION
    )


def test_content_hash_is_deterministic() -> None:
    """The same configuration always hashes identically."""

    first = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))
    second = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))
    assert configuration_content_hash(first) == configuration_content_hash(second)
    assert len(configuration_content_hash(first)) == 64


def test_content_hash_changes_with_the_configuration() -> None:
    """A different configuration is a different version."""

    base = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))
    changed = sanitized_configuration_snapshot(
        Settings.model_validate(valid_values(shadow_mode=False))
    )
    assert configuration_content_hash(base) != configuration_content_hash(changed)


def test_content_hash_ignores_the_secret_bearing_url() -> None:
    """Rotating a credential is not a configuration version change."""

    base = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))
    rotated = sanitized_configuration_snapshot(
        Settings.model_validate(
            valid_values(database_url="postgresql+psycopg://u:other@h/db")
        )
    )
    assert configuration_content_hash(base) == configuration_content_hash(rotated)


def test_snapshot_has_no_secret_like_key() -> None:
    """The snapshot passes the same guard used for persisted evidence."""

    from esl_service.domain.serialization import sanitize_evidence

    snapshot = sanitized_configuration_snapshot(Settings.model_validate(valid_values()))
    assert sanitize_evidence(snapshot) == snapshot


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_snapshot_works_for_every_non_production_environment(
    environment: str,
) -> None:
    """Snapshotting does not depend on Windows-only production validation."""

    settings = Settings.model_validate(valid_values(environment=environment))
    assert sanitized_configuration_snapshot(settings)["environment"] == environment
