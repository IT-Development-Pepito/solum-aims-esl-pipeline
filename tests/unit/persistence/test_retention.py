"""Retention policy safety rules (architecture 5.8).

Retention durations are UNKNOWN / NEEDS-DISCOVERY until the business supplies
them, so nothing is defaulted. Purge is disabled by default and refuses to run
without explicit configured durations.
"""

import os

import pytest

from esl_service.config import Settings
from esl_service.persistence.retention import RetentionPolicy


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ESL_ variables so these unit tests are deterministic."""

    for name in [key for key in os.environ if key.startswith("ESL_")]:
        monkeypatch.delenv(name, raising=False)


def settings(**overrides: object) -> Settings:
    """Build development settings, overriding only what a test needs."""

    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://user:pw@localhost/esl",
        "internal_host": "127.0.0.1",
        "shadow_mode": True,
    }
    values.update(overrides)
    return Settings.model_validate(values)


# --- nothing is defaulted ---------------------------------------------------


def test_purge_is_disabled_by_default() -> None:
    """A service that was never configured for purge must never delete."""

    policy = RetentionPolicy.from_settings(settings())
    assert policy.purge_enabled is False
    assert policy.detailed_evidence_days is None


def test_disabled_policy_needs_no_duration() -> None:
    """With purge disabled, absent durations are the expected state."""

    RetentionPolicy(
        purge_enabled=False,
        audit_core_days=None,
        detailed_evidence_days=None,
        compatibility_days=None,
    )


@pytest.mark.parametrize(
    "missing", ["audit_core_days", "detailed_evidence_days", "compatibility_days"]
)
def test_enabled_policy_requires_every_duration(missing: str) -> None:
    """Enabling purge without a duration names the missing key (FR-025)."""

    values: dict[str, int | None] = {
        "audit_core_days": 365,
        "detailed_evidence_days": 90,
        "compatibility_days": 30,
    }
    values[missing] = None

    with pytest.raises(ValueError, match=missing):
        RetentionPolicy(purge_enabled=True, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1])
def test_duration_must_be_positive(value: int) -> None:
    """A zero or negative retention period is never a valid configuration."""

    with pytest.raises(ValueError, match="detailed_evidence_days"):
        RetentionPolicy(
            purge_enabled=True,
            audit_core_days=365,
            detailed_evidence_days=value,
            compatibility_days=30,
        )


def test_detailed_evidence_age_is_derived_from_the_configured_days() -> None:
    """The eligibility age comes from configuration, never from a default."""

    policy = RetentionPolicy(
        purge_enabled=True,
        audit_core_days=365,
        detailed_evidence_days=90,
        compatibility_days=30,
    )
    assert policy.detailed_evidence_age.days == 90


def test_disabled_policy_has_no_derivable_age() -> None:
    """Asking for an age while purge is disabled is a programming error."""

    policy = RetentionPolicy.from_settings(settings())
    with pytest.raises(ValueError, match="detailed_evidence_days"):
        _ = policy.detailed_evidence_age


# --- settings carry the configuration ---------------------------------------


def test_settings_expose_retention_configuration() -> None:
    """Retention is externalised configuration, not a code constant."""

    configured = settings(
        retention_purge_enabled=True,
        audit_core_days=365,
        detailed_evidence_days=90,
        compatibility_days=30,
    )
    policy = RetentionPolicy.from_settings(configured)

    assert policy.purge_enabled is True
    assert policy.detailed_evidence_days == 90


def test_settings_reject_an_enabled_purge_without_durations() -> None:
    """Startup validation refuses an unsafe retention configuration."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="detailed_evidence_days"):
        settings(retention_purge_enabled=True, audit_core_days=365)


def test_retention_configuration_is_part_of_the_snapshot() -> None:
    """A configuration version records the retention settings it ran under."""

    from esl_service.config import sanitized_configuration_snapshot

    snapshot = sanitized_configuration_snapshot(settings())
    assert snapshot["retention_purge_enabled"] is False
