"""Immutable canonical snapshot and comparison evidence.

Snapshots are the durable inputs that make a comparison reproducible after a
restart or retry without a physical CSV file (FR-027). The canonical business
key is ``store_code + item_code + selling_uom`` (BR-018). Durable evidence uses
RESTRICT so audit history cannot be removed as a side effect of a delete.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from esl_service.persistence.models.base import Base
from esl_service.persistence.models.configuration import HASH_LENGTH

#: Representations that may be captured for comparison.
REPRESENTATION_KINDS = ("SOURCE_EXPECTED", "LEGACY_BASELINE", "AIMS_OBSERVED")

#: Difference classifications between a left and right canonical snapshot.
DIFFERENCE_TYPES = ("ADDED", "REMOVED", "CHANGED")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Render a SQL membership check for a controlled-vocabulary column."""

    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class SnapshotSet(Base):
    """One immutable capture of one representation for one execution."""

    __tablename__ = "snapshot_set"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "representation_kind",
            "adapter_name",
            "source_watermark",
            name="uq_snapshot_set_execution_representation_window",
        ),
        CheckConstraint(
            _in_clause("representation_kind", REPRESENTATION_KINDS),
            name="ck_snapshot_set_representation_kind",
        ),
        CheckConstraint("record_count >= 0", name="ck_snapshot_set_record_count"),
        CheckConstraint(
            f"aggregate_hash IS NULL OR char_length(aggregate_hash) = {HASH_LENGTH}",
            name="ck_snapshot_set_aggregate_hash_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    representation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_watermark: Mapped[str] = mapped_column(String(200), nullable=False)
    source_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canonical_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregate_hash: Mapped[str | None] = mapped_column(
        String(HASH_LENGTH), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalRecordSnapshot(Base):
    """A complete immutable canonical record retained as replay/audit evidence."""

    __tablename__ = "canonical_record_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_set_id",
            "store_code",
            "item_code",
            "selling_uom",
            name="uq_canonical_record_snapshot_key",
        ),
        CheckConstraint(
            f"char_length(canonical_hash) = {HASH_LENGTH}",
            name="ck_canonical_record_snapshot_hash_length",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_canonical_record_snapshot_payload_is_object",
        ),
        Index(
            "ix_canonical_record_snapshot_business_key",
            "store_code",
            "item_code",
            "selling_uom",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    snapshot_set_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("snapshot_set.id", ondelete="RESTRICT"),
        nullable=False,
    )
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    selling_uom: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordDifference(Base):
    """Deterministic comparison evidence between two canonical snapshots."""

    __tablename__ = "record_difference"
    __table_args__ = (
        CheckConstraint(
            _in_clause("difference_type", DIFFERENCE_TYPES),
            name="ck_record_difference_type",
        ),
        CheckConstraint(
            "left_snapshot_id IS NOT NULL OR right_snapshot_id IS NOT NULL",
            name="ck_record_difference_has_a_side",
        ),
        CheckConstraint(
            "jsonb_typeof(values_payload) = 'object'",
            name="ck_record_difference_values_is_object",
        ),
        Index("ix_record_difference_execution", "execution_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    left_snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("canonical_record_snapshot.id", ondelete="RESTRICT"),
        nullable=True,
    )
    right_snapshot_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("canonical_record_snapshot.id", ondelete="RESTRICT"),
        nullable=True,
    )
    left_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH), nullable=True)
    right_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH), nullable=True)
    difference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_paths: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    values_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diff_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
