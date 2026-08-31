"""Add promotion evaluation and candidate evidence.

Revision ID: 0004_promotion_evidence
Revises: 0003_execution_recovery
Create Date: 2026-08-31

Additive only. Retains every candidate an evaluation considered, so an
ambiguous or unresolved promotion decision stays auditable. No column encodes
a campaign-priority policy: selecting between several eligible candidates is
UNKNOWN / NEEDS-DISCOVERY and belongs to issue #37.

Money and structured promotion values use NUMERIC, never binary floating
point. Durable evidence uses RESTRICT.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_promotion_evidence"
down_revision: str | None = "0003_execution_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(19, 4)


def upgrade() -> None:
    """Create promotion evaluation and candidate evidence tables."""

    op.create_table(
        "promotion_evaluation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "canonical_record_snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column("calculation_version", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("selected_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "resulting_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('NO_PROMOTION', 'SELECTED', 'AMBIGUOUS', 'REJECTED', "
            "'UNRESOLVED')",
            name="ck_promotion_evaluation_outcome",
        ),
        sa.CheckConstraint(
            "resulting_state IS NULL OR jsonb_typeof(resulting_state) = 'object'",
            name="ck_promotion_evaluation_state_is_object",
        ),
        sa.CheckConstraint(
            "selected_candidate_id IS NULL OR outcome = 'SELECTED'",
            name="ck_promotion_evaluation_candidate_implies_selected",
        ),
        sa.CheckConstraint(
            "(outcome = 'SELECTED') OR resulting_state IS NULL",
            name="ck_promotion_evaluation_state_only_when_selected",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_record_snapshot_id"],
            ["canonical_record_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_record_snapshot_id",
            "rule_version",
            "calculation_version",
            name="uq_promotion_evaluation_snapshot_versions",
        ),
    )

    op.create_table(
        "promotion_candidate_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_campaign_id", sa.String(length=100), nullable=False),
        sa.Column("campaign_group", sa.String(length=100), nullable=True),
        sa.Column("promotion_type", sa.String(length=32), nullable=False),
        sa.Column("structured_value", MONEY, nullable=False),
        sa.Column("raw_disc_text", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weekday_evidence", sa.String(length=20), nullable=False),
        sa.Column("category_001_regular_price", MONEY, nullable=True),
        sa.Column("source_uom", sa.String(length=20), nullable=False),
        sa.Column("resolved_selling_uom", sa.String(length=20), nullable=True),
        sa.Column("calculated_effective_price", MONEY, nullable=True),
        sa.Column("display_price", MONEY, nullable=True),
        sa.Column("eligibility", sa.String(length=20), nullable=False),
        sa.Column("reason_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("fallback_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "eligibility IN ('ELIGIBLE', 'INELIGIBLE', 'REJECTED', 'UNRESOLVED')",
            name="ck_promotion_candidate_eligibility",
        ),
        sa.CheckConstraint(
            "weekday_evidence IN ('ACTIVE', 'INACTIVE', 'MISSING')",
            name="ck_promotion_candidate_weekday_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["promotion_evaluation.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_id",
            "source_campaign_id",
            name="uq_promotion_candidate_evaluation_campaign",
        ),
    )
    op.create_index(
        "ix_promotion_candidate_evaluation",
        "promotion_candidate_snapshot",
        ["evaluation_id"],
    )
    op.create_foreign_key(
        "fk_promotion_evaluation_selected_candidate",
        "promotion_evaluation",
        "promotion_candidate_snapshot",
        ["selected_candidate_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove promotion evidence."""

    op.drop_constraint(
        "fk_promotion_evaluation_selected_candidate",
        "promotion_evaluation",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_promotion_candidate_evaluation", table_name="promotion_candidate_snapshot"
    )
    op.drop_table("promotion_candidate_snapshot")
    op.drop_table("promotion_evaluation")
