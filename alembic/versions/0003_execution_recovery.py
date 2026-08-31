"""Add restart-safe execution state, steps, checkpoints, and lease lifecycle.

Revision ID: 0003_execution_recovery
Revises: 0002_configuration_and_snapshots
Create Date: 2026-08-31

Additive, except that the CASCADE foreign keys on ``execution_event`` and
``record_action`` are replaced with RESTRICT so durable audit evidence cannot
be removed as a side effect of deleting an execution.

The new ``workflow_execution`` columns are NOT NULL. That is safe because the
service has never run: ``workflow_execution`` is empty in every environment,
and the upgrade asserts this before altering the table rather than inventing
values for rows it cannot reconstruct.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_execution_recovery"
down_revision: str | None = "0002_configuration_and_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_LENGTH = 64


def _require_empty_executions() -> None:
    """Refuse to invent values for pre-existing executions."""

    existing = op.get_bind().execute(
        sa.text("SELECT count(*) FROM workflow_execution")
    ).scalar_one()
    if existing:
        raise RuntimeError(
            f"workflow_execution holds {existing} rows; 0003 adds NOT NULL columns "
            "that cannot be backfilled without an approved data decision."
        )


def upgrade() -> None:
    """Extend execution and lease state, then add steps and checkpoints."""

    _require_empty_executions()

    op.add_column(
        "workflow_execution",
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
    )
    op.add_column(
        "workflow_execution", sa.Column("mode", sa.String(length=10), nullable=False)
    )
    op.add_column(
        "workflow_execution",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("source_window_start", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("source_window_end", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "workflow_execution",
        sa.Column(
            "configuration_version_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("rule_version", sa.String(length=50), nullable=False),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("requested_by", sa.String(length=200), nullable=True),
    )
    op.add_column("workflow_execution", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column(
        "workflow_execution",
        sa.Column("retry_of_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workflow_execution",
        sa.Column(
            "replay_of_execution_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_execution",
        sa.Column("terminal_reason", sa.String(length=200), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflow_execution_configuration_version",
        "workflow_execution",
        "configuration_version",
        ["configuration_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_workflow_execution_retry_of",
        "workflow_execution",
        "workflow_execution",
        ["retry_of_execution_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_workflow_execution_replay_of",
        "workflow_execution",
        "workflow_execution",
        ["replay_of_execution_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_workflow_execution_source_window_order",
        "workflow_execution",
        "source_window_start <= source_window_end",
    )
    op.create_index("ix_workflow_execution_status", "workflow_execution", ["status"])
    op.create_index(
        "ix_workflow_execution_correlation", "workflow_execution", ["correlation_id"]
    )

    op.add_column(
        "scope_lease",
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "scope_lease",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now() + interval '15 minutes'"),
            nullable=False,
        ),
    )
    op.add_column(
        "scope_lease", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "scope_lease",
        sa.Column("lease_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.alter_column("scope_lease", "expires_at", server_default=None)
    op.alter_column("scope_lease", "lease_version", server_default=None)
    op.create_check_constraint(
        "ck_scope_lease_expiry_after_acquired", "scope_lease", "expires_at > acquired_at"
    )
    op.create_check_constraint(
        "ck_scope_lease_version", "scope_lease", "lease_version >= 1"
    )

    # Durable evidence must survive an attempt to delete its execution.
    for table, name, column in (
        ("scope_lease", "scope_lease_execution_id_fkey", "execution_id"),
        ("execution_event", "execution_event_execution_id_fkey", "execution_id"),
        ("record_action", "record_action_execution_id_fkey", "execution_id"),
    ):
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, "workflow_execution", [column], ["id"], ondelete="RESTRICT"
        )

    op.create_table(
        "execution_step",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_name", sa.String(length=100), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_execution_step_attempt"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "step_name",
            "attempt",
            name="uq_execution_step_execution_name_attempt",
        ),
    )
    op.create_index("ix_execution_step_execution", "execution_step", ["execution_id"])

    op.create_table(
        "execution_checkpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkpoint_key", sa.String(length=100), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("watermark", sa.String(length=200), nullable=False),
        sa.Column("payload_schema_version", sa.String(length=50), nullable=False),
        sa.Column("payload_hash", sa.String(length=HASH_LENGTH), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "checkpoint_version >= 1", name="ck_execution_checkpoint_version"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_execution_checkpoint_payload_is_object",
        ),
        sa.CheckConstraint(
            f"payload_hash IS NULL OR char_length(payload_hash) = {HASH_LENGTH}",
            name="ck_execution_checkpoint_payload_hash_length",
        ),
        sa.ForeignKeyConstraint(["step_id"], ["execution_step.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "step_id",
            "checkpoint_key",
            "checkpoint_version",
            name="uq_execution_checkpoint_step_key_version",
        ),
    )
    op.create_index(
        "ix_execution_checkpoint_step", "execution_checkpoint", ["step_id"]
    )


def downgrade() -> None:
    """Remove execution recovery state and restore the prior foreign keys."""

    op.drop_index("ix_execution_checkpoint_step", table_name="execution_checkpoint")
    op.drop_table("execution_checkpoint")
    op.drop_index("ix_execution_step_execution", table_name="execution_step")
    op.drop_table("execution_step")

    for table, name, column in (
        ("scope_lease", "scope_lease_execution_id_fkey", "execution_id"),
        ("execution_event", "execution_event_execution_id_fkey", "execution_id"),
        ("record_action", "record_action_execution_id_fkey", "execution_id"),
    ):
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, "workflow_execution", [column], ["id"], ondelete="CASCADE"
        )

    op.drop_constraint("ck_scope_lease_version", "scope_lease", type_="check")
    op.drop_constraint(
        "ck_scope_lease_expiry_after_acquired", "scope_lease", type_="check"
    )
    op.drop_column("scope_lease", "lease_version")
    op.drop_column("scope_lease", "released_at")
    op.drop_column("scope_lease", "expires_at")
    op.drop_column("scope_lease", "heartbeat_at")

    op.drop_index("ix_workflow_execution_correlation", table_name="workflow_execution")
    op.drop_index("ix_workflow_execution_status", table_name="workflow_execution")
    op.drop_constraint(
        "ck_workflow_execution_source_window_order", "workflow_execution", type_="check"
    )
    op.drop_constraint(
        "fk_workflow_execution_replay_of", "workflow_execution", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_workflow_execution_retry_of", "workflow_execution", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_workflow_execution_configuration_version",
        "workflow_execution",
        type_="foreignkey",
    )
    for column in (
        "terminal_reason",
        "ended_at",
        "replay_of_execution_id",
        "retry_of_execution_id",
        "reason",
        "requested_by",
        "rule_version",
        "configuration_version_id",
        "source_window_end",
        "source_window_start",
        "correlation_id",
        "mode",
        "trigger_type",
    ):
        op.drop_column("workflow_execution", column)
