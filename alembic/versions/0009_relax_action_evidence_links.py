"""Relax the two links that pinned detailed evidence under the audit core.

Revision ID: 0009_relax_action_evidence_links
Revises: 0008_authoritative_model_gate
Create Date: 2026-09-04

#62 delivered retention with a limitation it recorded rather than worked
around: `record_action.record_processing_result_id` and
`record_processing_result.canonical_record_snapshot_id` are `NOT NULL` with
`RESTRICT`, so retaining the audit core pinned the detailed rows beneath it
and a purge could never remove canonical snapshots, the largest class by
volume (architecture 5.8).

This makes exactly those two columns optional. `RESTRICT` stays: a purge
must null the link deliberately, and no delete ever cascades. Nulling loses
no audit value, because `record_action` already carries `store_code`,
`item_code`, `selling_uom`, `record_key`, `desired_state`, and
`idempotency_key`, so the action stays fully interpretable on its own.

Existing rows are untouched; relaxing a constraint rewrites no data.
"""

from collections.abc import Sequence

from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_relax_action_evidence_links"
down_revision: str | None = "0008_authoritative_model_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = (
    ("record_action", "record_processing_result_id"),
    ("record_processing_result", "canonical_record_snapshot_id"),
)


def upgrade() -> None:
    """Make both links optional, leaving their RESTRICT foreign keys in place."""

    for table, column in _LINKS:
        op.alter_column(
            table, column, existing_type=postgresql.UUID(as_uuid=True), nullable=True
        )


def downgrade() -> None:
    """Restore both NOT NULL constraints.

    This fails, by design, if a purge has already nulled a link: the detailed
    row it pointed at is gone and no value could be restored without inventing
    one. Recover such a database from backup rather than from this migration.
    """

    for table, column in _LINKS:
        op.alter_column(
            table, column, existing_type=postgresql.UUID(as_uuid=True), nullable=False
        )
