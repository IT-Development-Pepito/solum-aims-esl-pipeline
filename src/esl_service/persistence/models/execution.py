"""SQLAlchemy models for workflow execution state and audit records."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from esl_service.persistence.models.base import Base
from esl_service.persistence.models.configuration import HASH_LENGTH

if TYPE_CHECKING:
    from esl_service.persistence.models.actions import ActionAttempt


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
    # Set on RETRY_WAIT and cleared on RUNNING (0008): the worker does not pick
    # the execution before this instant, so a retry delay survives a restart.
    retry_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # Monotonic insertion order, for the same reason as audit_entry.
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordAction(Base):
    """The durable logical ledger of one external action (architecture 5.6).

    ``idempotency_key`` is globally unique, so a retry or restart resolves to
    the same row instead of duplicating an effect. A shadow execution may hold
    only INTENDED or SKIPPED_IDEMPOTENT, enforced here and in the domain.
    """

    __tablename__ = "record_action"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_record_action_idempotency_key"),
        CheckConstraint(
            f"char_length(idempotency_key) = {HASH_LENGTH}",
            name="ck_record_action_idempotency_key_length",
        ),
        CheckConstraint(
            "request_hash IS NULL OR "
            f"char_length(request_hash) = {HASH_LENGTH}",
            name="ck_record_action_request_hash_length",
        ),
        CheckConstraint(
            "state IN ('INTENDED', 'SKIPPED_IDEMPOTENT', 'SUBMITTING', "
            "'ACKNOWLEDGED', 'REJECTED', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', "
            "'OUTCOME_UNKNOWN')",
            name="ck_record_action_state",
        ),
        CheckConstraint("mode IN ('SHADOW', 'ACTIVE')", name="ck_record_action_mode"),
        # A shadow run must never record a submitted or acknowledged effect.
        CheckConstraint(
            "mode = 'ACTIVE' OR state IN ('INTENDED', 'SKIPPED_IDEMPOTENT')",
            name="ck_record_action_shadow_states",
        ),
        CheckConstraint(
            "desired_page IS NULL OR desired_page >= 0",
            name="ck_record_action_desired_page",
        ),
        Index("ix_record_action_execution", "execution_id"),
        Index("ix_record_action_state", "state"),
        Index(
            "ix_record_action_business_key", "store_code", "item_code", "selling_uom"
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
    record_processing_result_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("record_processing_result.id", ondelete="RESTRICT"),
        nullable=False,
    )
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    selling_uom: Mapped[str] = mapped_column(String(20), nullable=False)
    record_key: Mapped[str] = mapped_column(String(200), nullable=False)
    label_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    desired_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desired_state: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(
        String(HASH_LENGTH), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    source_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    acknowledgement_batch_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    terminal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempts: Mapped[list["ActionAttempt"]] = relationship(
        back_populates="action", order_by="ActionAttempt.attempt_number"
    )


class WorkflowSchedule(Base):
    """Persisted configuration for a workflow schedule."""

    __tablename__ = "workflow_schedule"
    __table_args__ = (
        # One active schedule per workflow and store (architecture 5.9); a
        # disabled schedule stays as history beside its replacement.
        Index(
            "uq_workflow_schedule_active_scope",
            "workflow_name",
            "store_code",
            unique=True,
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workflow_name: Mapped[str] = mapped_column(String(100), nullable=False)
    store_code: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expression: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Required since 0008; the gate refuses to run over a schedule without one.
    configuration_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("configuration_version.id", ondelete="RESTRICT"),
        nullable=False,
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
    # Monotonic start order (0008): a coarse host clock stamps a whole run's
    # steps with one instant, and the procedure's order must survive that.
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
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
