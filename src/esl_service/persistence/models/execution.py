"""SQLAlchemy models for workflow execution state and audit records."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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
from esl_service.persistence.models.configuration import HASH_LENGTH


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
        Index("ix_workflow_execution_status", "status"),
        Index("ix_workflow_execution_correlation", "correlation_id"),
        CheckConstraint(
            "source_window_start <= source_window_end",
            name="ck_workflow_execution_source_window_order",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    configuration_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("configuration_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=True,
    )
    replay_of_execution_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    terminal_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    steps: Mapped[list["ExecutionStep"]] = relationship(
        back_populates="execution",
        order_by="ExecutionStep.started_at, ExecutionStep.attempt",
    )


class ScopeLease(Base):
    """Exclusive ownership of a workflow scope while it is active."""

    __tablename__ = "scope_lease"
    __table_args__ = (
        CheckConstraint(
            "expires_at > acquired_at", name="ck_scope_lease_expiry_after_acquired"
        ),
        CheckConstraint("lease_version >= 1", name="ck_scope_lease_version"),
    )

    scope_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExecutionEvent(Base):
    """Immutable structured event associated with a workflow execution."""

    __tablename__ = "execution_event"
    __table_args__ = (Index("ix_execution_event_execution_occurred", "execution_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
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
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
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
    # Nullable until 0008 makes it required after an explicit backfill and
    # preflight; new application writes must already supply a version.
    configuration_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("configuration_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExecutionStep(Base):
    """One attempt at one named step of an execution (FR-007, FR-014)."""

    __tablename__ = "execution_step"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "step_name",
            "attempt",
            name="uq_execution_step_execution_name_attempt",
        ),
        CheckConstraint("attempt >= 1", name="ck_execution_step_attempt"),
        Index("ix_execution_step_execution", "execution_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workflow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    execution: Mapped["WorkflowExecution"] = relationship(back_populates="steps")
    checkpoints: Mapped[list["ExecutionCheckpoint"]] = relationship(
        back_populates="step",
        order_by="ExecutionCheckpoint.occurred_at, ExecutionCheckpoint.checkpoint_version",
    )


class ExecutionCheckpoint(Base):
    """Append-only durable progress marker a restart can resume from (FR-010).

    Checkpoints make recovery possible without reading a local CSV file
    (FR-027). A checkpoint is never edited: a later position is a new row.
    """

    __tablename__ = "execution_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "checkpoint_key",
            "checkpoint_version",
            name="uq_execution_checkpoint_step_key_version",
        ),
        CheckConstraint(
            "checkpoint_version >= 1", name="ck_execution_checkpoint_version"
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_execution_checkpoint_payload_is_object",
        ),
        CheckConstraint(
            f"payload_hash IS NULL OR char_length(payload_hash) = {HASH_LENGTH}",
            name="ck_execution_checkpoint_payload_hash_length",
        ),
        Index("ix_execution_checkpoint_step", "step_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    step_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("execution_step.id", ondelete="RESTRICT"),
        nullable=False,
    )
    checkpoint_key: Mapped[str] = mapped_column(String(100), nullable=False)
    checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    watermark: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(
        String(HASH_LENGTH), nullable=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    step: Mapped["ExecutionStep"] = relationship(back_populates="checkpoints")
