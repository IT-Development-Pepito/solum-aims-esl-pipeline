"""Add configuration, canonical snapshot, and difference evidence.

Revision ID: 0002_configuration_and_snapshots
Revises: 0001_operational_state
Create Date: 2026-08-28

Additive only. Durable evidence uses RESTRICT so audit history cannot be
removed as a side effect of deleting an execution. ``workflow_schedule``
gains a nullable configuration version that revision 0008 makes required
after an explicit backfill and preflight.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_configuration_and_snapshots"
down_revision: str | None = "0001_operational_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_LENGTH = 64


def upgrade() -> None:
    """Create configuration, snapshot, and difference tables additively."""

    op.create_table(
        "store_configuration",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("options_schema_version", sa.String(length=50), nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(options) = 'object'",
            name="ck_store_configuration_options_is_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_code", name="uq_store_configuration_store_code"),
    )

    op.create_table(
        "configuration_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("content_hash", sa.String(length=HASH_LENGTH), nullable=False),
        sa.Column(
            "sanitized_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_by", sa.String(length=200), nullable=False),
        sa.CheckConstraint(
            f"char_length(content_hash) = {HASH_LENGTH}",
            name="ck_configuration_version_hash_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sanitized_snapshot) = 'object'",
            name="ck_configuration_version_snapshot_is_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment",
            "content_hash",
            name="uq_configuration_version_environment_hash",
        ),
    )

    op.create_table(
        "snapshot_set",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("representation_kind", sa.String(length=32), nullable=False),
        sa.Column("adapter_name", sa.String(length=100), nullable=False),
        sa.Column("source_watermark", sa.String(length=200), nullable=False),
        sa.Column("source_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canonical_schema_version", sa.String(length=50), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("aggregate_hash", sa.String(length=HASH_LENGTH), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "representation_kind IN ('SOURCE_EXPECTED', 'LEGACY_BASELINE', 'AIMS_OBSERVED')",
            name="ck_snapshot_set_representation_kind",
        ),
        sa.CheckConstraint("record_count >= 0", name="ck_snapshot_set_record_count"),
        sa.CheckConstraint(
            f"aggregate_hash IS NULL OR char_length(aggregate_hash) = {HASH_LENGTH}",
            name="ck_snapshot_set_aggregate_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "representation_kind",
            "adapter_name",
            "source_watermark",
            name="uq_snapshot_set_execution_representation_window",
        ),
    )

    op.create_table(
        "canonical_record_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("selling_uom", sa.String(length=20), nullable=False),
        sa.Column("canonical_schema_version", sa.String(length=50), nullable=False),
        sa.Column("canonical_hash", sa.String(length=HASH_LENGTH), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"char_length(canonical_hash) = {HASH_LENGTH}",
            name="ck_canonical_record_snapshot_hash_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_canonical_record_snapshot_payload_is_object",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_set_id"], ["snapshot_set.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_set_id",
            "store_code",
            "item_code",
            "selling_uom",
            name="uq_canonical_record_snapshot_key",
        ),
    )
    op.create_index(
        "ix_canonical_record_snapshot_business_key",
        "canonical_record_snapshot",
        ["store_code", "item_code", "selling_uom"],
    )

    op.create_table(
        "record_difference",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("left_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("right_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("left_hash", sa.String(length=HASH_LENGTH), nullable=True),
        sa.Column("right_hash", sa.String(length=HASH_LENGTH), nullable=True),
        sa.Column("difference_type", sa.String(length=32), nullable=False),
        sa.Column("changed_paths", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "values_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("diff_schema_version", sa.String(length=50), nullable=False),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "difference_type IN ('ADDED', 'REMOVED', 'CHANGED')",
            name="ck_record_difference_type",
        ),
        sa.CheckConstraint(
            "left_snapshot_id IS NOT NULL OR right_snapshot_id IS NOT NULL",
            name="ck_record_difference_has_a_side",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(values_payload) = 'object'",
            name="ck_record_difference_values_is_object",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["left_snapshot_id"],
            ["canonical_record_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["right_snapshot_id"],
            ["canonical_record_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_record_difference_execution", "record_difference", ["execution_id"])

    op.add_column(
        "workflow_schedule",
        sa.Column("configuration_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workflow_schedule",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_workflow_schedule_configuration_version",
        "workflow_schedule",
        "configuration_version",
        ["configuration_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove the additive configuration and evidence schema."""

    op.drop_constraint(
        "fk_workflow_schedule_configuration_version",
        "workflow_schedule",
        type_="foreignkey",
    )
    op.drop_column("workflow_schedule", "updated_at")
    op.drop_column("workflow_schedule", "configuration_version_id")

    op.drop_index("ix_record_difference_execution", table_name="record_difference")
    op.drop_table("record_difference")
    op.drop_index(
        "ix_canonical_record_snapshot_business_key",
        table_name="canonical_record_snapshot",
    )
    op.drop_table("canonical_record_snapshot")
    op.drop_table("snapshot_set")
    op.drop_table("configuration_version")
    op.drop_table("store_configuration")
