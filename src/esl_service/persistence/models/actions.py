"""Append-only delivery attempts for one logical external action.

An attempt records what was tried and, crucially, whether the request is known
to have reached the external system. An unknown attempt is reconciled before
any resend, so an ambiguous submission is never blindly repeated (FR-013,
FR-016, architecture 5.6).
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from esl_service.persistence.models.base import Base
from esl_service.persistence.models.evidence import _in_clause
from esl_service.persistence.models.execution import RecordAction

DELIVERY_CERTAINTIES = ("CONFIRMED", "NOT_DELIVERED", "UNKNOWN")


class ActionAttempt(Base):
    """One attempt at delivering an action, retained as evidence."""

    __tablename__ = "action_attempt"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "attempt_number", name="uq_action_attempt_action_number"
        ),
        CheckConstraint("attempt_number >= 1", name="ck_action_attempt_number"),
        CheckConstraint(
            _in_clause("delivery_certainty", DELIVERY_CERTAINTIES),
            name="ck_action_attempt_delivery_certainty",
        ),
        CheckConstraint(
            "jsonb_typeof(response_evidence) = 'object'",
            name="ck_action_attempt_response_is_object",
        ),
        Index("ix_action_attempt_action", "action_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    action_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("record_action.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_certainty: Mapped[str] = mapped_column(String(20), nullable=False)
    retry_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    response_evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    action: Mapped["RecordAction"] = relationship(back_populates="attempts")
