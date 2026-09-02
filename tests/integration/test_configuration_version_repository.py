"""Registering the active configuration version at startup (FR-002, FR-025, #28).

Every execution references the configuration it ran under. Until now only
tests created ``configuration_version`` rows; the host must register the
sanitized snapshot of its own settings when it starts, reusing the existing
row when the content hash is unchanged so a restart does not multiply
versions and a changed setting produces a new one.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.config import Settings
from esl_service.persistence.configuration_repository import ConfigurationRepository
from esl_service.persistence.models import ConfigurationVersion

BASE = {
    "environment": "development",
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl_pipeline_dev",
    "internal_host": "127.0.0.1",
}


def settings(**overrides: object) -> Settings:
    return Settings.model_validate({**BASE, **overrides})


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ConfigurationVersion)) or 0


def test_the_first_registration_creates_a_version_carrying_the_snapshot(
    session: Session,
) -> None:
    version = ConfigurationRepository(session).ensure_active(settings(), activated_by="service")

    assert isinstance(version.id, UUID)
    assert version.environment == "development"
    assert version.activated_by == "service"
    assert version.sanitized_snapshot["shadow_mode"] is True
    assert "database_url" not in version.sanitized_snapshot
    assert count(session) == 1


def test_an_unchanged_configuration_reuses_the_existing_version(session: Session) -> None:
    repository = ConfigurationRepository(session)

    first = repository.ensure_active(settings(), activated_by="service")
    second = repository.ensure_active(settings(), activated_by="service")

    assert second.id == first.id
    assert count(session) == 1


def test_a_changed_setting_produces_a_new_version(session: Session) -> None:
    repository = ConfigurationRepository(session)

    first = repository.ensure_active(settings(), activated_by="service")
    second = repository.ensure_active(
        settings(operator_roles="pepito=admin"), activated_by="service"
    )

    assert second.id != first.id
    assert second.content_hash != first.content_hash
    assert count(session) == 2


def test_a_rotated_secret_does_not_produce_a_new_version(session: Session) -> None:
    """The snapshot excludes secret-bearing settings, so rotation is invisible here."""

    repository = ConfigurationRepository(session)

    first = repository.ensure_active(settings(), activated_by="service")
    second = repository.ensure_active(
        settings(secret_bundle_path=r"C:\ProgramData\SOLUM\ESL\other.dpapi"),
        activated_by="service",
    )

    assert second.id == first.id
