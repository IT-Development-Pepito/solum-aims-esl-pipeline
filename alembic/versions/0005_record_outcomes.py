"""Add per-record processing outcomes and their issues.

Revision ID: 0005_record_outcomes
Revises: 0004_promotion_evidence
Create Date: 2026-08-31

Additive only. Retains rejected and unresolved records with their reasons so
quarantined work is countable, traceable, and replayable after a correction.

A database check enforces that a quarantined record carries no action
decision, so invalid or unresolved work cannot reach an external effect even
if an application path is added later. Durable evidence uses RESTRICT.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_record_outcomes"
down_revision: str | None = "0004_promotion_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create record outcome and issue tables."""

    op.create_table(
        "record_processing_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_record_snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("selling_uom", sa.String(length=20), nullable=False),
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("eligibility_status", sa.String(length=20), nullable=False),
        sa.Column("promotion_outcome", sa.String(length=20), nullable=True),
        sa.Column("current_page", sa.Integer(), nullable=True),
        sa.Column("desired_page", sa.Integer(), nullable=True),
        sa.Column("action_decision", sa.String(length=20), nullable=False),
        sa.Column("processing_status", sa.String(length=20), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "validation_status IN ('VALID', 'REJECTED')",
            name="ck_record_processing_result_validation_status",
        ),
        sa.CheckConstraint(
            "eligibility_status IN ('ELIGIBLE', 'INELIGIBLE', 'UNRESOLVED')",
            name="ck_record_processing_result_eligibility_status",
        ),
        sa.CheckConstraint(
            "action_decision IN ('NONE', 'PAGE_CHANGE', 'SKIP_IDEMPOTENT')",
            name="ck_record_processing_result_action_decision",
        ),
        sa.CheckConstraint(
            "processing_status IN ('REJECTED', 'UNRESOLVED', 'INELIGIBLE', "
            "'UNCHANGED', 'ACTION_REQUIRED')",
            name="ck_record_processing_result_processing_status",
        ),
        sa.CheckConstraint(
            "promotion_outcome IS NULL OR promotion_outcome IN ('NO_PROMOTION', "
            "'SELECTED', 'AMBIGUOUS', 'REJECTED', 'UNRESOLVED')",
            name="ck_record_processing_result_promotion_outcome",
        ),
        sa.CheckConstraint(
            "action_decision = 'NONE' OR "
            "(validation_status = 'VALID' AND eligibility_status <> 'UNRESOLVED')",
            name="ck_record_processing_result_quarantine_has_no_action",
        ),
        sa.CheckConstraint(
            "current_page IS NULL OR current_page >= 0",
            name="ck_record_processing_result_current_page",
        ),
        sa.CheckConstraint(
            "desired_page IS NULL OR desired_page >= 0",
            name="ck_record_processing_result_desired_page",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_record_snapshot_id"],
            ["canonical_record_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "store_code",
            "item_code",
            "selling_uom",
            name="uq_record_processing_result_execution_key",
        ),
    )
    op.create_index(
        "ix_record_processing_result_execution",
        "record_processing_result",
        ["execution_id"],
    )
    op.create_index(
        "ix_record_processing_result_status",
        "record_processing_result",
        ["execution_id", "processing_status"],
    )
    op.create_index(
        "ix_record_processing_result_business_key",
        "record_processing_result",
        ["store_code", "item_code", "selling_uom"],
    )

    op.create_table(
        "record_issue",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.String(length=50), nullable=False),
        sa.Column("issue_code", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("classification", sa.String(length=50), nullable=False),
        sa.Column("evidence_schema_version", sa.String(length=50), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_record_issue_sequence"),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name="ck_record_issue_evidence_is_object",
        ),
        sa.UniqueConstraint(
            "result_id", "sequence", name="uq_record_issue_result_sequence"
        ),
        sa.ForeignKeyConstraint(
            ["result_id"], ["record_processing_result.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_record_issue_result", "record_issue", ["result_id"])
    op.create_index("ix_record_issue_code", "record_issue", ["issue_code"])


def downgrade() -> None:
    """Remove record outcomes and issues."""

    op.drop_index("ix_record_issue_code", table_name="record_issue")
    op.drop_index("ix_record_issue_result", table_name="record_issue")
    op.drop_table("record_issue")
    op.drop_index(
        "ix_record_processing_result_business_key",
        table_name="record_processing_result",
    )
    op.drop_index(
        "ix_record_processing_result_status", table_name="record_processing_result"
    )
    op.drop_index(
        "ix_record_processing_result_execution", table_name="record_processing_result"
    )
    op.drop_table("record_processing_result")
