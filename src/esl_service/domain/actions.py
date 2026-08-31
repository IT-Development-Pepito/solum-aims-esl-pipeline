"""Logical idempotency keys and the external action lifecycle (FR-013).

The state graph below is exactly the one documented in
``docs/SYSTEM_ARCHITECTURE.md`` section 5.6. Two properties matter most:

* the same logical action always resolves to the same idempotency key, so a
  retry or a restart produces one logical outcome rather than a duplicate;
* ``OUTCOME_UNKNOWN`` has **no** automatic path back to ``SUBMITTING``. An
  ambiguous submission is reconciled by an operator before any resend, so the
  service never blindly repeats an effect it cannot account for.

A shadow execution may reach only ``INTENDED`` or ``SKIPPED_IDEMPOTENT``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.serialization import (
    JSONValue,
    canonical_hash,
    sanitize_evidence,
)


class ActionState(StrEnum):
    """Lifecycle of one logical external action (architecture 5.6)."""

    INTENDED = "INTENDED"
    SKIPPED_IDEMPOTENT = "SKIPPED_IDEMPOTENT"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class DeliveryCertainty(StrEnum):
    """Whether one attempt is known to have reached the external system."""

    CONFIRMED = "CONFIRMED"
    NOT_DELIVERED = "NOT_DELIVERED"
    UNKNOWN = "UNKNOWN"


_ALLOWED_ACTION_TRANSITIONS: Mapping[ActionState, frozenset[ActionState]] = {
    ActionState.INTENDED: frozenset(
        {ActionState.SKIPPED_IDEMPOTENT, ActionState.SUBMITTING}
    ),
    ActionState.SUBMITTING: frozenset(
        {
            ActionState.ACKNOWLEDGED,
            ActionState.REJECTED,
            ActionState.FAILED_RETRYABLE,
            ActionState.FAILED_TERMINAL,
            ActionState.OUTCOME_UNKNOWN,
        }
    ),
    ActionState.FAILED_RETRYABLE: frozenset({ActionState.SUBMITTING}),
    # OUTCOME_UNKNOWN is deliberately absent: it is operator-action-required
    # and must be reconciled before any resend.
}

_TERMINAL_ACTION_STATES = frozenset(
    {
        ActionState.SKIPPED_IDEMPOTENT,
        ActionState.ACKNOWLEDGED,
        ActionState.REJECTED,
        ActionState.FAILED_TERMINAL,
    }
)

#: States a shadow execution may reach; it never causes an external effect.
_SHADOW_ACTION_STATES = frozenset(
    {ActionState.INTENDED, ActionState.SKIPPED_IDEMPOTENT}
)


@dataclass(frozen=True)
class ActionTransitionEvent:
    """Structured evidence persistence appends to the action audit trail."""

    event_type: str
    previous_state: ActionState
    requested_state: ActionState
    reason_code: str

    @property
    def payload(self) -> dict[str, str]:
        """Return the stable, secret-free event payload."""

        return {
            "from_state": self.previous_state.value,
            "to_state": self.requested_state.value,
            "reason_code": self.reason_code,
        }


class InvalidActionTransition(ValueError):
    """Reject an invalid action state change while retaining evidence."""

    def __init__(self, audit_event: ActionTransitionEvent) -> None:
        self.audit_event = audit_event
        super().__init__(
            "invalid action transition: "
            f"{audit_event.previous_state.value} -> "
            f"{audit_event.requested_state.value} "
            f"({audit_event.reason_code})"
        )


def transition_action(
    previous_state: ActionState,
    requested_state: ActionState,
    *,
    mode: ExecutionMode,
) -> ActionTransitionEvent:
    """Validate one action transition and return structured audit evidence."""

    if mode is ExecutionMode.SHADOW and requested_state not in _SHADOW_ACTION_STATES:
        raise InvalidActionTransition(
            ActionTransitionEvent(
                event_type="ACTION_TRANSITION_REJECTED",
                previous_state=previous_state,
                requested_state=requested_state,
                reason_code="SHADOW_ACTION_MAY_NOT_SUBMIT",
            )
        )

    if requested_state not in _ALLOWED_ACTION_TRANSITIONS.get(
        previous_state, frozenset()
    ):
        raise InvalidActionTransition(
            ActionTransitionEvent(
                event_type="ACTION_TRANSITION_REJECTED",
                previous_state=previous_state,
                requested_state=requested_state,
                reason_code="INVALID_ACTION_TRANSITION",
            )
        )

    return ActionTransitionEvent(
        event_type="ACTION_TRANSITION_ACCEPTED",
        previous_state=previous_state,
        requested_state=requested_state,
        reason_code="ACTION_TRANSITION_ACCEPTED",
    )


def is_terminal_action(state: ActionState) -> bool:
    """Return whether an action state permits no further transition."""

    return state in _TERMINAL_ACTION_STATES


def requires_reconciliation(state: ActionState) -> bool:
    """Return whether a state blocks progress until an operator resolves it."""

    return state is ActionState.OUTCOME_UNKNOWN


@dataclass(frozen=True)
class NewRecordAction:
    """One logical external action requested for one canonical record.

    ``execution_id`` and ``record_processing_result_id`` identify which run
    asked for the action, but are deliberately **not** part of the idempotency
    key: a retry or restart must resolve to the same logical action.
    """

    execution_id: UUID
    record_processing_result_id: UUID
    key: CanonicalKey
    label_code: str | None
    action_type: str
    desired_page: int | None
    desired_state: str
    mode: ExecutionMode
    contract_version: str
    rule_version: str
    configuration_hash: str
    source_window_start: datetime
    source_window_end: datetime
    request_hash: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("action_type", self.action_type),
            ("desired_state", self.desired_state),
            ("contract_version", self.contract_version),
            ("rule_version", self.rule_version),
            ("configuration_hash", self.configuration_hash),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")

        for name, moment in (
            ("source_window_start", self.source_window_start),
            ("source_window_end", self.source_window_end),
        ):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")

        if self.source_window_start > self.source_window_end:
            raise ValueError("source_window_start must not follow source_window_end")

        if self.desired_page is not None and self.desired_page < 0:
            raise ValueError("desired_page must not be negative")


@dataclass(frozen=True)
class _IdempotencyInput:
    """Exactly the fields that define an action's logical identity.

    A typed constructor rather than a dictionary, because the canonical
    serializer deliberately refuses untyped mappings: canonical payloads must
    come from declared contracts so a field cannot be added by accident.
    """

    contract_version: str
    key: CanonicalKey
    label_code: str | None
    action_type: str
    desired_state: str
    rule_version: str
    configuration_hash: str
    source_window_start: datetime
    source_window_end: datetime


def build_idempotency_key(action: NewRecordAction) -> str:
    """Return the stable logical identity of one external action (FR-013).

    Derived from the approved adapter contract, the logical business and
    action key, the desired state, the rule and configuration versions, and
    the reproducible source window. Secret and volatile transport values —
    including ``request_hash`` and any execution identity — are excluded, so
    the same logical action keeps one identity across retries and restarts.
    """

    return canonical_hash(
        _IdempotencyInput(
            contract_version=action.contract_version,
            key=action.key,
            label_code=action.label_code,
            action_type=action.action_type,
            desired_state=action.desired_state,
            rule_version=action.rule_version,
            configuration_hash=action.configuration_hash,
            source_window_start=action.source_window_start,
            source_window_end=action.source_window_end,
        )
    )


@dataclass(frozen=True)
class ActionAttemptEvidence:
    """One append-only attempt at delivering an action to an external system."""

    attempt_number: int
    started_at: datetime
    ended_at: datetime | None
    delivery_certainty: DeliveryCertainty
    retry_class: str | None
    result_code: str | None
    error_class: str | None
    response_evidence: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("attempt_number must start at 1")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        # Raises rather than redacting, so a leaking caller is fixed at source.
        sanitize_evidence(dict(self.response_evidence))
