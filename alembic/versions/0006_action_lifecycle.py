"""Add the idempotent action ledger and its delivery attempts.

Revision ID: 0006_action_lifecycle
Revises: 0005_record_outcomes
Create Date: 2026-08-31

Extends record_action into the durable logical action ledger of architecture
5.6 and adds append-only action_attempt.

``idempotency_key`` is globally unique, so a retry or restart resolves to the
existing action instead of duplicating an external effect. A database check
also enforces that a shadow execution can hold only INTENDED or
SKIPPED_IDEMPOTENT, so a shadow run can never record a submitted effect.

The new record_action columns are NOT NULL. That is safe because the service
has never run: record_action is empty in every environment, and the upgrade
asserts this rather than inventing an idempotency key it cannot reconstruct.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_action_lifecycle"
down_revision: str | None = "0005_record_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_LENGTH = 64


def _require_empty_actions() -> None:
    """Refuse to invent an idempotency key for a pre-existing action."""

    existing = op.get_bind().execute(
        sa.text("SELECT count(*) FROM record_action")
    ).scalar_one()
    if existing:
        raise RuntimeError(
            f"record_action holds {existing} rows; 0006 adds NOT NULL columns "
            "including a logical idempotency key that cannot be reconstructed "
            "without an approved data decision."
        )


def upgrade() -> None:
    """Extend the action ledger and add its attempt evidence."""

    _require_empty_actions()

    for column in (
        sa.Column("record_processing_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_code", sa.String(length=20), nullable=False),
        sa.Column("item_code", sa.String(length=50), nullable=False),
        sa.Column("selling_uom", sa.String(length=20), nullable=False),
        sa.Column("label_code", sa.String(length=100), nullable=True),
        sa.Column("desired_page", sa.Integer(), nullable=True),
        sa.Column("desired_state", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=HASH_LENGTH), nullable=False),
        sa.Column("request_hash", sa.String(length=HASH_LENGTH), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("contract_version", sa.String(length=50), nullable=False),
        sa.Column("rule_version", sa.String(length=50), nullable=False),
        sa.Column("configuration_hash", sa.String(length=HASH_LENGTH), nullable=False),
        sa.Column("source_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledgement_batch_id", sa.String(length=100), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("record_action", column)

    op.add_column(
        "record_action",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_foreign_key(
        "fk_record_action_record_processing_result",
        "record_action",
        "record_processing_result",
        ["record_processing_result_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_record_action_idempotency_key", "record_action", ["idempotency_key"]
    )
    op.create_check_constraint(
        "ck_record_action_idempotency_key_length",
        "record_action",
        f"char_length(idempotency_key) = {HASH_LENGTH}",
    )
    op.create_check_constraint(
        "ck_record_action_request_hash_length",
        "record_action",
        f"request_hash IS NULL OR char_length(request_hash) = {HASH_LENGTH}",
    )
    op.create_check_constraint(
        "ck_record_action_state",
        "record_action",
        "state IN ('INTENDED', 'SKIPPED_IDEMPOTENT', 'SUBMITTING', 'ACKNOWLEDGED', "
        "'REJECTED', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'OUTCOME_UNKNOWN')",
    )
    op.create_check_constraint(
        "ck_record_action_mode", "record_action", "mode IN ('SHADOW', 'ACTIVE')"
    )
    op.create_check_constraint(
        "ck_record_action_shadow_states",
        "record_action",
        "mode = 'ACTIVE' OR state IN ('INTENDED', 'SKIPPED_IDEMPOTENT')",
    )
    op.create_check_constraint(
        "ck_record_action_desired_page",
        "record_action",
        "desired_page IS NULL OR desired_page >= 0",
    )
    op.create_index("ix_record_action_execution", "record_action", ["execution_id"])
    op.create_index("ix_record_action_state", "record_action", ["state"])
    op.create_index(
        "ix_record_action_business_key",
        "record_action",
        ["store_code", "item_code", "selling_uom"],
    )

    op.create_table(
        "action_attempt",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_certainty", sa.String(length=20), nullable=False),
        sa.Column("retry_class", sa.String(length=32), nullable=True),
        sa.Column("result_code", sa.String(length=50), nullable=True),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column("response_schema_version", sa.String(length=50), nullable=False),
        sa.Column(
            "response_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_action_attempt_number"),
        sa.CheckConstraint(
            "delivery_certainty IN ('CONFIRMED', 'NOT_DELIVERED', 'UNKNOWN')",
            name="ck_action_attempt_delivery_certainty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response_evidence) = 'object'",
            name="ck_action_attempt_response_is_object",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["record_action.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_id", "attempt_number", name="uq_action_attempt_action_number"
        ),
    )
    op.create_index("ix_action_attempt_action", "action_attempt", ["action_id"])


def downgrade() -> None:
    """Remove the action ledger extension and its attempts."""

    op.drop_index("ix_action_attempt_action", table_name="action_attempt")
    op.drop_table("action_attempt")

    op.drop_index("ix_record_action_business_key", table_name="record_action")
    op.drop_index("ix_record_action_state", table_name="record_action")
    op.drop_index("ix_record_action_execution", table_name="record_action")
    for name in (
        "ck_record_action_desired_page",
        "ck_record_action_shadow_states",
        "ck_record_action_mode",
        "ck_record_action_state",
        "ck_record_action_request_hash_length",
        "ck_record_action_idempotency_key_length",
    ):
        op.drop_constraint(name, "record_action", type_="check")
    op.drop_constraint(
        "uq_record_action_idempotency_key", "record_action", type_="unique"
    )
    op.drop_constraint(
        "fk_record_action_record_processing_result", "record_action", type_="foreignkey"
    )
    for column in (
        "updated_at",
        "terminal_at",
        "acknowledgement_batch_id",
        "source_window_end",
        "source_window_start",
        "configuration_hash",
        "rule_version",
        "contract_version",
        "mode",
        "state",
        "request_hash",
        "idempotency_key",
        "desired_state",
        "desired_page",
        "label_code",
        "selling_uom",
        "item_code",
        "store_code",
        "record_processing_result_id",
    ):
        op.drop_column("record_action", column)
