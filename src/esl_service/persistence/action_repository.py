"""Repository for the idempotent external-action ledger (FR-013).

``create_intended`` is idempotent by the action's logical key, so a repeated
submission or a restart resolves to the existing row rather than duplicating
an effect. A duplicate is recorded as an auditable decision instead of being
silently discarded.

``transition`` validates against the documented state graph and uses
compare-and-set, so a second worker acting on a stale state is refused. No
method commits a caller's transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from esl_service.domain.actions import (
    ActionAttemptEvidence,
    ActionState,
    NewRecordAction,
    build_idempotency_key,
    is_terminal_action,
    transition_action,
)
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.serialization import canonical_payload, sanitize_evidence
from esl_service.persistence.models import (
    ActionAttempt,
    ExecutionEvent,
    RecordAction,
)

#: Schema version of the sanitized attempt response evidence.
RESPONSE_SCHEMA_VERSION = "action-response-v1"


@dataclass(frozen=True)
class _ActionPayload:
    """The sanitized, secret-free description of one intended action."""

    action_type: str
    label_code: str | None
    desired_page: int | None
    desired_state: str
    contract_version: str

    @classmethod
    def of(cls, request: NewRecordAction) -> "_ActionPayload":
        """Build the stored payload from an action request."""

        return cls(
            action_type=request.action_type,
            label_code=request.label_code,
            desired_page=request.desired_page,
            desired_state=request.desired_state,
            contract_version=request.contract_version,
        )


class ConcurrentActionUpdate(RuntimeError):
    """Raised when an action's state changed under a caller's feet.

    The compare-and-set update matched no row, so another worker already moved
    the action. The caller must re-read state rather than retrying blindly.
    """


class ActionRepository:
    """Persists intended actions, their transitions, and their attempts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_intended(self, request: NewRecordAction) -> RecordAction:
        """Return the action for this logical key, creating it only once.

        Uses INSERT ... ON CONFLICT DO NOTHING followed by a SELECT, so two
        concurrent callers converge on one row. A duplicate appends an
        ``ACTION_DUPLICATE_DETECTED`` event, keeping the context that a second
        request was made rather than discarding it.
        """

        idempotency_key = build_idempotency_key(request)
        statement = (
            insert(RecordAction)
            .values(
                execution_id=request.execution_id,
                record_processing_result_id=request.record_processing_result_id,
                store_code=request.key.store_code,
                item_code=request.key.item_code,
                selling_uom=request.key.selling_uom,
                record_key=(
                    f"{request.key.store_code}:{request.key.item_code}:"
                    f"{request.key.selling_uom}"
                ),
                label_code=request.label_code,
                action_type=request.action_type,
                desired_page=request.desired_page,
                desired_state=request.desired_state,
                idempotency_key=idempotency_key,
                request_hash=request.request_hash,
                state=ActionState.INTENDED.value,
                mode=request.mode.value,
                contract_version=request.contract_version,
                rule_version=request.rule_version,
                configuration_hash=request.configuration_hash,
                source_window_start=request.source_window_start,
                source_window_end=request.source_window_end,
                payload=cast(
                    "dict[str, object]",
                    canonical_payload(_ActionPayload.of(request)),
                ),
            )
            .on_conflict_do_nothing(index_elements=[RecordAction.idempotency_key])
        )
        created_id = self._session.scalars(
            statement.returning(RecordAction.id)
        ).one_or_none()
        self._session.flush()

        existing = self._session.scalars(
            select(RecordAction).where(
                RecordAction.idempotency_key == idempotency_key
            )
        ).one()

        if created_id is None:
            self._session.add(
                ExecutionEvent(
                    execution_id=request.execution_id,
                    event_type="ACTION_DUPLICATE_DETECTED",
                    payload={
                        "idempotency_key": idempotency_key,
                        "existing_action_id": str(existing.id),
                        "existing_state": existing.state,
                    },
                )
            )
            self._session.flush()
        return existing

    def transition(
        self,
        action_id: UUID,
        requested_state: ActionState,
        *,
        expected_state: ActionState | None = None,
        acknowledgement_batch_id: str | None = None,
    ) -> RecordAction:
        """Apply a validated state change with compare-and-set semantics."""

        action = self._session.get_one(RecordAction, action_id)
        previous = expected_state or ActionState(action.state)
        transition_action(previous, requested_state, mode=ExecutionMode(action.mode))

        values: dict[str, object] = {"state": requested_state.value}
        if acknowledgement_batch_id is not None:
            values["acknowledgement_batch_id"] = acknowledgement_batch_id
        if is_terminal_action(requested_state):
            values["terminal_at"] = datetime.now(UTC)

        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(RecordAction)
                .where(
                    RecordAction.id == action_id,
                    RecordAction.state == previous.value,
                )
                .values(**values)
            ),
        )
        if result.rowcount != 1:
            raise ConcurrentActionUpdate(
                f"action {action_id} was not in {previous.value}"
            )
        self._session.flush()
        self._session.expire(action)
        return self._session.get_one(RecordAction, action_id)

    def append_attempt(
        self, action_id: UUID, attempt: ActionAttemptEvidence
    ) -> ActionAttempt:
        """Append one delivery attempt as immutable evidence."""

        stored = ActionAttempt(
            action_id=action_id,
            attempt_number=attempt.attempt_number,
            started_at=attempt.started_at,
            ended_at=attempt.ended_at,
            delivery_certainty=attempt.delivery_certainty.value,
            retry_class=attempt.retry_class,
            result_code=attempt.result_code,
            error_class=attempt.error_class,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            # Re-checked here so persistence cannot store a secret even if a
            # caller built the contract by another route.
            response_evidence=cast(
                "dict[str, object]",
                sanitize_evidence(dict(attempt.response_evidence)),
            ),
        )
        self._session.add(stored)
        self._session.flush()
        return stored

    def unresolved_actions(self) -> list[RecordAction]:
        """Return actions whose external outcome is unknown (FR-013).

        These block completion and must be reconciled by an operator; they are
        never resubmitted automatically.
        """

        statement = (
            select(RecordAction)
            .where(RecordAction.state == ActionState.OUTCOME_UNKNOWN.value)
            .order_by(RecordAction.occurred_at, RecordAction.id)
            .options(selectinload(RecordAction.attempts))
        )
        return list(self._session.scalars(statement))
