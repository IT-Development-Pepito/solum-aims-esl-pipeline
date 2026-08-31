"""Audit ledger and reconciliation reports (FR-021, FR-022).

A finalized report is immutable: re-reconciling an execution creates another
revision rather than overwriting evidence. Every imbalance and unresolved
external effect is enumerated as its own exception row, so nothing is hidden
inside an aggregate count.

The audit ledger is append-only and may exist without an execution, because
schedule and configuration actions are audited too.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
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

RECONCILIATION_MODES = ("ACTIVE", "SHADOW")
REPORT_STATUSES = ("DRAFT", "FINALIZED")
RESOLUTION_STATUSES = ("OPEN", "RESOLVED", "ACCEPTED")

#: Count columns every report must carry, used for the non-negative checks.
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


class AuditEntry(Base):
    """Append-only record of who did what, when, why, and to what outcome."""

    __tablename__ = "audit_entry"
    __table_args__ = (
        CheckConstraint(
            "before_evidence IS NULL OR jsonb_typeof(before_evidence) = 'object'",
            name="ck_audit_entry_before_is_object",
        ),
        CheckConstraint(
            "after_evidence IS NULL OR jsonb_typeof(after_evidence) = 'object'",
            name="ck_audit_entry_after_is_object",
        ),
        Index("ix_audit_entry_execution", "execution_id"),
        Index("ix_audit_entry_resource", "resource_type", "resource_key"),
        Index("ix_audit_entry_occurred", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # Monotonic insertion order. Several entries share one timestamp, so
    # without it an audit trail's order would fall back to a random UUID.
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    # Nullable: a schedule or configuration action is audited without a run.
    execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=True,
    )
    configuration_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("configuration_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    correlation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(200), nullable=False)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    evidence_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    before_evidence: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    after_evidence: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReconciliationReport(Base):
    """One balanced accounting of an execution, immutable once finalized."""

    __tablename__ = "reconciliation_report"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "revision", name="uq_reconciliation_report_revision"
        ),
        CheckConstraint("revision >= 1", name="ck_reconciliation_report_revision"),
        CheckConstraint(
            _in_clause("mode", RECONCILIATION_MODES),
            name="ck_reconciliation_report_mode",
        ),
        CheckConstraint(
            _in_clause("status", REPORT_STATUSES),
            name="ck_reconciliation_report_status",
        ),
        *(
            CheckConstraint(f"{name} >= 0", name=f"ck_reconciliation_report_{name}")
            for name in COUNT_COLUMNS
        ),
        Index("ix_reconciliation_report_execution", "execution_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    extracted: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    valid: Mapped[int] = mapped_column(Integer, nullable=False)
    ineligible: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible: Mapped[int] = mapped_column(Integer, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_idempotent: Mapped[int] = mapped_column(Integer, nullable=False)
    intended: Mapped[int] = mapped_column(Integer, nullable=False)
    acknowledged: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_by_aims: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted: Mapped[int] = mapped_column(Integer, nullable=False)
    ambiguous: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    exceptions: Mapped[list["ReconciliationException"]] = relationship(
        back_populates="report", order_by="ReconciliationException.sequence"
    )


class ReconciliationException(Base):
    """One enumerated imbalance or unresolved effect within a report."""

    __tablename__ = "reconciliation_exception"
    __table_args__ = (
        UniqueConstraint(
            "report_id", "sequence", name="uq_reconciliation_exception_sequence"
        ),
        CheckConstraint("sequence >= 0", name="ck_reconciliation_exception_sequence"),
        CheckConstraint(
            _in_clause("resolution_status", RESOLUTION_STATUSES),
            name="ck_reconciliation_exception_resolution_status",
        ),
        CheckConstraint(
            "expected_evidence IS NULL OR "
            "jsonb_typeof(expected_evidence) = 'object'",
            name="ck_reconciliation_exception_expected_is_object",
        ),
        CheckConstraint(
            "actual_evidence IS NULL OR jsonb_typeof(actual_evidence) = 'object'",
            name="ck_reconciliation_exception_actual_is_object",
        ),
        Index("ix_reconciliation_exception_report", "report_id"),
        Index("ix_reconciliation_exception_category", "category"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    report_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("reconciliation_report.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    record_processing_result_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("record_processing_result.id", ondelete="RESTRICT"),
        nullable=True,
    )
    record_action_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("record_action.id", ondelete="RESTRICT"),
        nullable=True,
    )
    store_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    item_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    selling_uom: Mapped[str | None] = mapped_column(String(20), nullable=True)
    expected_evidence: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    actual_evidence: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    report: Mapped["ReconciliationReport"] = relationship(back_populates="exceptions")
