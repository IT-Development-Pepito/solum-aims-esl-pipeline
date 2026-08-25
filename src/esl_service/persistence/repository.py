"""Repository operations for service-owned workflow state."""
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from esl_service.persistence.models import (
    ExecutionEvent,
    RecordAction,
    ScopeLease,
    WorkflowExecution,
)


class ExecutionRepository:
    """Persists workflow execution state without committing a caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_execution(
        self, workflow_name: str, store_code: str, started_at: str
    ) -> WorkflowExecution:
        """Create and flush an execution so subsequent actions can reference its UUID."""

        execution = WorkflowExecution(
            workflow_name=workflow_name,
            store_code=store_code,
            started_at=_parse_utc_timestamp(started_at),
        )
        self._session.add(execution)
        self._session.flush()
        return execution

    def claim_scope(self, execution_id: UUID, scope_key: str) -> bool:
        """Claim a scope exactly once, returning false when another execution owns it."""

        statement = (
            insert(ScopeLease)
            .values(scope_key=scope_key, execution_id=execution_id)
            .on_conflict_do_nothing(index_elements=[ScopeLease.scope_key])
        )
        inserted_scope_key = self._session.scalars(
            statement.returning(ScopeLease.scope_key)
        ).one_or_none()
        return inserted_scope_key is not None

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


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse the documented ISO-8601 input and normalize it to UTC."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("started_at must include a UTC offset")
    return parsed.astimezone(UTC)
