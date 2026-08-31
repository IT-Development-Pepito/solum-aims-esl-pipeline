"""Per-record processing outcomes and their independently queryable issues.

A record that was rejected or left unresolved is retained with its reasons, so
quarantined work is countable, traceable, and replayable after a correction
(FR-003, FR-006, FR-021, FR-022). The canonical key is stored relationally so
outcomes can be queried by store, item, and selling UOM without opening JSON.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from esl_service.persistence.models.base import Base
from esl_service.persistence.models.evidence import _in_clause

VALIDATION_STATUSES = ("VALID", "REJECTED")
ELIGIBILITY_STATUSES = ("ELIGIBLE", "INELIGIBLE", "UNRESOLVED")
ACTION_DECISIONS = ("NONE", "PAGE_CHANGE", "SKIP_IDEMPOTENT")
PROCESSING_STATUSES = (
    "REJECTED",
    "UNRESOLVED",
    "INELIGIBLE",
    "UNCHANGED",
    "ACTION_REQUIRED",
)
PROMOTION_OUTCOME_VALUES = (
    "NO_PROMOTION",
    "SELECTED",
    "AMBIGUOUS",
    "REJECTED",
    "UNRESOLVED",
)


class RecordProcessingResult(Base):
    """What happened to one canonical record in one execution."""

    __tablename__ = "record_processing_result"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "store_code",
            "item_code",
            "selling_uom",
            name="uq_record_processing_result_execution_key",
        ),
        CheckConstraint(
            _in_clause("validation_status", VALIDATION_STATUSES),
            name="ck_record_processing_result_validation_status",
        ),
        CheckConstraint(
            _in_clause("eligibility_status", ELIGIBILITY_STATUSES),
            name="ck_record_processing_result_eligibility_status",
        ),
        CheckConstraint(
            _in_clause("action_decision", ACTION_DECISIONS),
            name="ck_record_processing_result_action_decision",
        ),
        CheckConstraint(
            _in_clause("processing_status", PROCESSING_STATUSES),
            name="ck_record_processing_result_processing_status",
        ),
        CheckConstraint(
            "promotion_outcome IS NULL OR "
            + _in_clause("promotion_outcome", PROMOTION_OUTCOME_VALUES),
            name="ck_record_processing_result_promotion_outcome",
        ),
        # A quarantined record must never carry an external action decision.
        CheckConstraint(
            "action_decision = 'NONE' OR "
            "(validation_status = 'VALID' AND eligibility_status <> 'UNRESOLVED')",
            name="ck_record_processing_result_quarantine_has_no_action",
        ),
        CheckConstraint(
            "current_page IS NULL OR current_page >= 0",
            name="ck_record_processing_result_current_page",
        ),
        CheckConstraint(
            "desired_page IS NULL OR desired_page >= 0",
            name="ck_record_processing_result_desired_page",
        ),
        Index("ix_record_processing_result_execution", "execution_id"),
        Index(
            "ix_record_processing_result_status", "execution_id", "processing_status"
        ),
        Index(
            "ix_record_processing_result_business_key",
            "store_code",
            "item_code",
            "selling_uom",
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
    canonical_record_snapshot_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("canonical_record_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    selling_uom: Mapped[str] = mapped_column(String(20), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    eligibility_status: Mapped[str] = mapped_column(String(20), nullable=False)
    promotion_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desired_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_decision: Mapped[str] = mapped_column(String(20), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    issues: Mapped[list["RecordIssue"]] = relationship(
        back_populates="result", order_by="RecordIssue.sequence"
    )


class RecordIssue(Base):
    """One independently queryable reason a record was not processed cleanly.

    Multiple issues per record are expected: a record can be unresolved for an
    unconvertible UOM and a missing regular price at the same time, and both
    must remain separately visible.
    """

    __tablename__ = "record_issue"
    __table_args__ = (
        # An explicit ordinal: several issues share one timestamp, so without
        # it the order the rules produced them would be lost.
        UniqueConstraint(
            "result_id", "sequence", name="uq_record_issue_result_sequence"
        ),
        CheckConstraint("sequence >= 0", name="ck_record_issue_sequence"),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name="ck_record_issue_evidence_is_object",
        ),
        Index("ix_record_issue_result", "result_id"),
        Index("ix_record_issue_code", "issue_code"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    result_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("record_processing_result.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(50), nullable=False)
    issue_code: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    classification: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    result: Mapped["RecordProcessingResult"] = relationship(back_populates="issues")
