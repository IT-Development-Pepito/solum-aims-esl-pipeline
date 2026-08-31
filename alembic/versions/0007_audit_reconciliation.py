"""Add the audit ledger and reconciliation reports.

Revision ID: 0007_audit_reconciliation
Revises: 0006_action_lifecycle
Create Date: 2026-08-31

Additive only. A finalized reconciliation report is immutable: re-reconciling
an execution creates another revision rather than overwriting evidence, so the
unique constraint is on execution plus revision.

Every imbalance and unresolved external effect is enumerated as its own
exception row rather than hidden in an aggregate count. Durable evidence uses
RESTRICT.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_audit_reconciliation"
down_revision: str | None = "0006_action_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNT_COLUMNS = (
    "extracted",
    "rejected",
    "valid",
    "ineligible",
    "eligible",
    "unchanged",
    "skipped_idempotent",
    "intended",
    "acknowledged",
    "rejected_by_aims",
    "failed",
    "unresolved",
    "submitted",
    "ambiguous",
)


def upgrade() -> None:
    """Create the audit ledger, reconciliation reports, and their exceptions."""

    # execution_event gains a monotonic ordinal so a queried event trail has a
    # deterministic order: several events share one timestamp, and ordering by
    # a random UUID is not an auditable sequence.
    op.add_column(
        "execution_event",
        sa.Column(
            "sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
    )
    op.create_unique_constraint(
        "uq_execution_event_sequence", "execution_event", ["sequence"]
    )

    op.create_table(
        "audit_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "configuration_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_key", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=50), nullable=False),
        sa.Column("evidence_schema_version", sa.String(length=50), nullable=False),
        sa.Column(
            "before_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "after_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "before_evidence IS NULL OR jsonb_typeof(before_evidence) = 'object'",
            name="ck_audit_entry_before_is_object",
        ),
        sa.CheckConstraint(
            "after_evidence IS NULL OR jsonb_typeof(after_evidence) = 'object'",
            name="ck_audit_entry_after_is_object",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["configuration_version_id"],
            ["configuration_version.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence", name="uq_audit_entry_sequence"),
    )
    op.create_index("ix_audit_entry_execution", "audit_entry", ["execution_id"])
    op.create_index(
        "ix_audit_entry_resource", "audit_entry", ["resource_type", "resource_key"]
    )
    op.create_index("ix_audit_entry_occurred", "audit_entry", ["occurred_at"])

    op.create_table(
        "reconciliation_report",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *(sa.Column(name, sa.Integer(), nullable=False) for name in COUNT_COLUMNS),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision >= 1", name="ck_reconciliation_report_revision"),
        sa.CheckConstraint(
            "mode IN ('ACTIVE', 'SHADOW')", name="ck_reconciliation_report_mode"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'FINALIZED')", name="ck_reconciliation_report_status"
        ),
        *(
            sa.CheckConstraint(f"{name} >= 0", name=f"ck_reconciliation_report_{name}")
            for name in COUNT_COLUMNS
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["workflow_execution.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "revision", name="uq_reconciliation_report_revision"
        ),
    )
    op.create_index(
        "ix_reconciliation_report_execution", "reconciliation_report", ["execution_id"]
    )

    op.create_table(
        "reconciliation_exception",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column(
            "record_processing_result_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("record_action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("store_code", sa.String(length=20), nullable=True),
        sa.Column("item_code", sa.String(length=50), nullable=True),
        sa.Column("selling_uom", sa.String(length=20), nullable=True),
        sa.Column(
            "expected_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "actual_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("resolution_status", sa.String(length=20), nullable=False),
        sa.Column("resolved_by", sa.String(length=200), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence >= 0", name="ck_reconciliation_exception_sequence"
        ),
        sa.CheckConstraint(
            "resolution_status IN ('OPEN', 'RESOLVED', 'ACCEPTED')",
            name="ck_reconciliation_exception_resolution_status",
        ),
        sa.CheckConstraint(
            "expected_evidence IS NULL OR jsonb_typeof(expected_evidence) = 'object'",
            name="ck_reconciliation_exception_expected_is_object",
        ),
        sa.CheckConstraint(
            "actual_evidence IS NULL OR jsonb_typeof(actual_evidence) = 'object'",
            name="ck_reconciliation_exception_actual_is_object",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reconciliation_report.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["record_processing_result_id"],
            ["record_processing_result.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["record_action_id"], ["record_action.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_id", "sequence", name="uq_reconciliation_exception_sequence"
        ),
    )
    op.create_index(
        "ix_reconciliation_exception_report", "reconciliation_exception", ["report_id"]
    )
    op.create_index(
        "ix_reconciliation_exception_category", "reconciliation_exception", ["category"]
    )


def downgrade() -> None:
    """Remove reconciliation reports and the audit ledger."""

    op.drop_index(
        "ix_reconciliation_exception_category", table_name="reconciliation_exception"
    )
    op.drop_index(
        "ix_reconciliation_exception_report", table_name="reconciliation_exception"
    )
    op.drop_table("reconciliation_exception")
    op.drop_index(
        "ix_reconciliation_report_execution", table_name="reconciliation_report"
    )
    op.drop_table("reconciliation_report")
    op.drop_index("ix_audit_entry_occurred", table_name="audit_entry")
    op.drop_index("ix_audit_entry_resource", table_name="audit_entry")
    op.drop_index("ix_audit_entry_execution", table_name="audit_entry")
    op.drop_table("audit_entry")
    op.drop_constraint(
        "uq_execution_event_sequence", "execution_event", type_="unique"
    )
    op.drop_column("execution_event", "sequence")
