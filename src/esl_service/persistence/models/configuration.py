"""Configured stores and immutable, secret-free configuration versions.

Adding a store is configuration rather than a code change (FR-026). Every
execution references exactly one immutable configuration version (FR-002,
FR-010, FR-025). Neither table may hold credentials or secret values.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from esl_service.persistence.models.base import Base

#: Length of a lowercase SHA-256 hexadecimal digest.
HASH_LENGTH = 64


class StoreConfiguration(Base):
    """One configured store scope that the workflow may process."""

    __tablename__ = "store_configuration"
    __table_args__ = (
        UniqueConstraint("store_code", name="uq_store_configuration_store_code"),
        CheckConstraint(
            "jsonb_typeof(options) = 'object'",
            name="ck_store_configuration_options_is_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    options_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    options: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ConfigurationVersion(Base):
    """An immutable, sanitized snapshot of non-secret configuration content."""

    __tablename__ = "configuration_version"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "content_hash",
            name="uq_configuration_version_environment_hash",
        ),
        CheckConstraint(
            f"char_length(content_hash) = {HASH_LENGTH}",
            name="ck_configuration_version_hash_length",
        ),
        CheckConstraint(
            "jsonb_typeof(sanitized_snapshot) = 'object'",
            name="ck_configuration_version_snapshot_is_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    sanitized_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_by: Mapped[str] = mapped_column(String(200), nullable=False)
