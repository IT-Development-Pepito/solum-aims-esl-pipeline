"""SQLAlchemy models for workflow state and audit records."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for the service-owned PostgreSQL state schema."""


class WorkflowExecution(Base):
    """One attempt to process a workflow for a store scope."""

    __tablename__ = "workflow_execution"
    __table_args__ = (
        Index(
            "ix_workflow_execution_workflow_store_started",
            "workflow_name",
            "store_code",
            "started_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScopeLease(Base):
    """Exclusive ownership of a workflow scope while it is active."""

    __tablename__ = "scope_lease"

    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="CASCADE"),
        nullable=False,
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionEvent(Base):
    """Immutable structured event associated with a workflow execution."""

    __tablename__ = "execution_event"
    __table_args__ = (Index("ix_execution_event_execution_occurred", "execution_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordAction(Base):
    """One intended or completed record-level action in an execution."""

    __tablename__ = "record_action"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="CASCADE"),
        nullable=False,
    )
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowSchedule(Base):
    """Persisted configuration for a workflow schedule."""

    __tablename__ = "workflow_schedule"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expression: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
