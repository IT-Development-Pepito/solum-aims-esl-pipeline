"""Registering the active configuration version (FR-002, FR-025, #28).

Every execution references the configuration it ran under. The host
registers the sanitized snapshot of its own settings when it starts and
reuses the existing row when the content hash is unchanged, so a restart
does not multiply versions and a changed setting produces a new one. The
snapshot excludes secret-bearing settings (#27), so rotating a credential is
invisible here by design.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.config import (
    CONFIGURATION_SCHEMA_VERSION,
    Settings,
    configuration_content_hash,
    sanitized_configuration_snapshot,
)
from esl_service.persistence.models import ConfigurationVersion


class ConfigurationRepository:
    """Finds or records the configuration version a host runs under."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure_active(self, settings: Settings, *, activated_by: str) -> ConfigurationVersion:
        """Return the version for these settings, creating it on first sight."""

        snapshot = sanitized_configuration_snapshot(settings)
        content_hash = configuration_content_hash(snapshot)
        existing = self._session.scalars(
            select(ConfigurationVersion).where(
                ConfigurationVersion.environment == settings.environment,
                ConfigurationVersion.content_hash == content_hash,
            )
        ).first()
        if existing is not None:
            return existing

        version = ConfigurationVersion(
            environment=settings.environment,
            schema_version=CONFIGURATION_SCHEMA_VERSION,
            content_hash=content_hash,
            sanitized_snapshot=dict(snapshot),
            activated_by=activated_by,
        )
        self._session.add(version)
        self._session.flush()
        return version
