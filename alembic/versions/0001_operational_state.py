"""Create durable workflow state and audit tables.

Revision ID: 0001_operational_state
Revises:
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_operational_state"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create service-owned workflow state, lease, audit, and schedule tables."""

    op.create_table(
        "workflow_execution",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_name", sa.String(length=100), nullable=False),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_execution_workflow_store_started",
        "workflow_execution",
        ["workflow_name", "store_code", "started_at"],
    )
    op.create_table(
        "scope_lease",
        sa.Column("scope_key", sa.String(length=200), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_execution.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scope_key"),
    )
    op.create_table(
        "execution_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_execution.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_event_execution_occurred",
        "execution_event",
        ["execution_id", "occurred_at"],
    )
    op.create_table(
        "record_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_key", sa.String(length=200), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["workflow_execution.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "workflow_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_name", sa.String(length=100), nullable=False),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("cron_expression", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Remove the Task 3 service-owned state schema."""

    op.drop_table("workflow_schedule")
    op.drop_table("record_action")
    op.drop_index("ix_execution_event_execution_occurred", table_name="execution_event")
    op.drop_table("execution_event")
    op.drop_table("scope_lease")
    op.drop_index(
        "ix_workflow_execution_workflow_store_started",
        table_name="workflow_execution",
    )
    op.drop_table("workflow_execution")
