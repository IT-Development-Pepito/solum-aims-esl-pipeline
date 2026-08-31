"""Repository operations for service-owned workflow state.

Execution transitions are validated by the pure-domain contract in
:mod:`esl_service.domain.workflow`; this layer persists the result and appends
its audit evidence in the same transaction. No method commits a caller's
transaction.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from esl_service.domain.outcomes import FailureClass, NewExecution
from esl_service.domain.workflow import (
    ExecutionStatus,
    is_terminal,
    transition_execution,
)
from esl_service.persistence.models import (
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionStep,
    RecordAction,
    ScopeLease,
    WorkflowExecution,
)

#: How long a scope lease stays valid without a heartbeat.
DEFAULT_LEASE_DURATION = timedelta(minutes=15)

#: Statuses an interrupted execution can still be recovered from.
RECOVERABLE_STATUSES = (
    ExecutionStatus.QUEUED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.RETRY_WAIT,
    ExecutionStatus.RECOVERING,
)


class ConcurrentExecutionUpdate(RuntimeError):
    """Raised when an execution's status changed under a caller's feet.

    The compare-and-set update matched no row, so another worker already moved
    the execution. The caller must re-read state rather than retrying blindly.
    """


class ExecutionRepository:
    """Persists workflow execution state without committing a caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_execution(self, request: NewExecution) -> WorkflowExecution:
        """Create a QUEUED execution from its complete, reproducible input scope."""

        execution = WorkflowExecution(
            workflow_name=request.workflow_name,
            store_code=request.store_code,
            trigger_type=request.trigger_type.value,
            mode=request.mode.value,
            correlation_id=request.correlation_id,
            source_window_start=request.source_window_start,
            source_window_end=request.source_window_end,
            configuration_version_id=request.configuration_version_id,
            rule_version=request.rule_version,
            requested_by=request.requested_by,
            reason=request.reason,
            retry_of_execution_id=request.retry_of_execution_id,
            replay_of_execution_id=request.replay_of_execution_id,
            started_at=datetime.now(UTC),
            status=ExecutionStatus.QUEUED.value,
        )
        self._session.add(execution)
        self._session.flush()
        return execution

    def transition_execution(
        self,
        execution_id: UUID,
        expected_status: ExecutionStatus,
        requested_status: ExecutionStatus,
        *,
        terminal_reason: str | None = None,
    ) -> WorkflowExecution:
        """Apply a validated transition and append its evidence atomically.

        The domain graph rejects an invalid change before any write. A rejected
        transition raises without persisting, and its exception carries the
        audit event so the caller can record it on a transaction it controls.
        """

        audit_event = transition_execution(expected_status, requested_status)
        now = datetime.now(UTC)
        values: dict[str, object] = {"status": requested_status.value}
        if terminal_reason is not None:
            values["terminal_reason"] = terminal_reason
        if is_terminal(requested_status):
            values["ended_at"] = now

        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WorkflowExecution)
            .where(
                WorkflowExecution.id == execution_id,
                WorkflowExecution.status == expected_status.value,
            )
                .values(**values)
            ),
        )
        if result.rowcount != 1:
            raise ConcurrentExecutionUpdate(
                f"execution {execution_id} was not in {expected_status.value}"
            )

        self._session.add(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=audit_event.event_type,
                payload=dict(audit_event.payload),
            )
        )
        self._session.flush()
        return self._session.get_one(WorkflowExecution, execution_id)

    def recoverable_executions(self) -> list[WorkflowExecution]:
        """Return non-terminal executions a restart must resume or reconcile."""

        statement = (
            select(WorkflowExecution)
            .where(
                WorkflowExecution.status.in_(
                    [status.value for status in RECOVERABLE_STATUSES]
                )
            )
            .order_by(WorkflowExecution.started_at, WorkflowExecution.id)
            .options(
                selectinload(WorkflowExecution.steps).selectinload(
                    ExecutionStep.checkpoints
                )
            )
        )
        return list(self._session.scalars(statement))

    def claim_scope(
        self,
        execution_id: UUID,
        scope_key: str,
        *,
        now: datetime | None = None,
        duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> bool:
        """Claim a scope exactly once, returning false when another execution owns it."""

        moment = now or datetime.now(UTC)
        values = {
            "scope_key": scope_key,
            "execution_id": execution_id,
            "acquired_at": moment,
            "heartbeat_at": moment,
            "expires_at": moment + duration,
            "lease_version": 1,
        }
        # One row per scope. A released or expired lease may be taken over; a
        # live one is never stolen, so only one execution owns a scope at a time.
        statement = (
            insert(ScopeLease)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[ScopeLease.scope_key],
                set_={key: value for key, value in values.items() if key != "scope_key"}
                | {"released_at": None},
                where=(
                    ScopeLease.released_at.is_not(None) | (ScopeLease.expires_at <= moment)
                ),
            )
        )
        claimed = self._session.scalars(
            statement.returning(ScopeLease.scope_key)
        ).one_or_none()
        self._session.expire_all()
        return claimed is not None

    def heartbeat_scope(
        self,
        scope_key: str,
        execution_id: UUID,
        *,
        now: datetime | None = None,
        duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> bool:
        """Extend a live lease so recovery does not reclaim active work."""

        moment = now or datetime.now(UTC)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ScopeLease)
                .where(
                    ScopeLease.scope_key == scope_key,
                    ScopeLease.execution_id == execution_id,
                    ScopeLease.released_at.is_(None),
                )
                .values(
                    heartbeat_at=moment,
                    expires_at=moment + duration,
                    lease_version=ScopeLease.lease_version + 1,
                )
            ),
        )
        return result.rowcount == 1

    def release_scope(
        self, scope_key: str, execution_id: UUID, *, now: datetime | None = None
    ) -> bool:
        """Release a scope so the next execution can take it over.

        The row is retained with ``released_at`` set, so the handover is
        visible rather than silently deleted.
        """

        moment = now or datetime.now(UTC)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ScopeLease)
                .where(
                    ScopeLease.scope_key == scope_key,
                    ScopeLease.execution_id == execution_id,
                    ScopeLease.released_at.is_(None),
                )
                .values(released_at=moment)
            ),
        )
        if result.rowcount != 1:
            return False
        self._session.expire_all()
        return True

    def expired_leases(self, *, now: datetime | None = None) -> list[ScopeLease]:
        """Return leases whose owner stopped heartbeating before its expiry."""

        moment = now or datetime.now(UTC)
        statement = (
            select(ScopeLease)
            .where(ScopeLease.released_at.is_(None), ScopeLease.expires_at <= moment)
            .order_by(ScopeLease.scope_key)
        )
        return list(self._session.scalars(statement))

    def get_lease(self, scope_key: str) -> ScopeLease | None:
        """Return the current lease for a scope, if one is held."""

        return self._session.get(ScopeLease, scope_key)

    def start_step(
        self, execution_id: UUID, step_name: str, *, attempt: int = 1
    ) -> ExecutionStep:
        """Record one attempt at a named step as RUNNING."""

        step = ExecutionStep(
            execution_id=execution_id,
            step_name=step_name,
            attempt=attempt,
            outcome="RUNNING",
            started_at=datetime.now(UTC),
        )
        self._session.add(step)
        self._session.flush()
        return step

    def finish_step(
        self,
        step_id: UUID,
        *,
        outcome: str,
        failure_class: FailureClass | None = None,
    ) -> ExecutionStep:
        """Close one step attempt, retaining it as evidence for later attempts."""

        step = self._session.get_one(ExecutionStep, step_id)
        step.outcome = outcome
        step.failure_class = None if failure_class is None else failure_class.value
        step.ended_at = datetime.now(UTC)
        self._session.flush()
        return step

    def append_checkpoint(
        self,
        step_id: UUID,
        *,
        checkpoint_key: str,
        checkpoint_version: int,
        watermark: str,
        payload: Mapping[str, object],
        payload_schema_version: str = "checkpoint-v1",
        payload_hash: str | None = None,
    ) -> ExecutionCheckpoint:
        """Append a durable progress marker a restart can resume from."""

        checkpoint = ExecutionCheckpoint(
            step_id=step_id,
            checkpoint_key=checkpoint_key,
            checkpoint_version=checkpoint_version,
            watermark=watermark,
            payload_schema_version=payload_schema_version,
            payload_hash=payload_hash,
            payload=dict(payload),
            occurred_at=datetime.now(UTC),
        )
        self._session.add(checkpoint)
        self._session.flush()
        return checkpoint

    def append_event(
        self, execution_id: UUID, event_type: str, payload: Mapping[str, object]
    ) -> ExecutionEvent:
        """Append an immutable structured event to an execution audit trail."""

        event = ExecutionEvent(
            execution_id=execution_id,
            event_type=event_type,
            payload=dict(payload),
        )
        self._session.add(event)
        self._session.flush()
        return event

    def record_action(
        self,
        execution_id: UUID,
        record_key: str,
        action_type: str,
        payload: Mapping[str, object],
    ) -> RecordAction:
        """Record a durable record-level action for later reconciliation."""

        action = RecordAction(
            execution_id=execution_id,
            record_key=record_key,
            action_type=action_type,
            payload=dict(payload),
        )
        self._session.add(action)
        self._session.flush()
        return action

    def list_events(self, execution_id: UUID) -> list[ExecutionEvent]:
        """Return events in their database occurrence order for an execution."""

        statement = (
            select(ExecutionEvent)
            .where(ExecutionEvent.execution_id == execution_id)
            .order_by(ExecutionEvent.occurred_at, ExecutionEvent.id)
        )
        return list(self._session.scalars(statement))
