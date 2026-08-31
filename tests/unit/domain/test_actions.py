"""Action lifecycle and logical idempotency keys (FR-013).

The state graph is the one documented in SYSTEM_ARCHITECTURE.md section 5.6.
An unknown submission is never resubmitted blindly: OUTCOME_UNKNOWN is
operator-action-required and has no automatic path back to SUBMITTING.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from esl_service.domain.actions import (
    ActionState,
    DeliveryCertainty,
    InvalidActionTransition,
    NewRecordAction,
    build_idempotency_key,
    is_terminal_action,
    requires_reconciliation,
    transition_action,
)
from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import ExecutionMode

KEY = CanonicalKey("084", "101024011793", "KGS")
WINDOW_START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 28, 7, 30, tzinfo=UTC)


def action(**overrides: object) -> NewRecordAction:
    """Build one intended action request."""

    values: dict[str, object] = {
        "execution_id": uuid4(),
        "record_processing_result_id": uuid4(),
        "key": KEY,
        "label_code": "LBL-0001",
        "action_type": "PAGE_CHANGE",
        "desired_page": 2,
        "desired_state": "PAGE_2",
        "mode": ExecutionMode.ACTIVE,
        "contract_version": "aims-page-v1",
        "rule_version": "rules-v1",
        "configuration_hash": "a" * 64,
        "source_window_start": WINDOW_START,
        "source_window_end": WINDOW_END,
    }
    values.update(overrides)
    return NewRecordAction(**values)  # type: ignore[arg-type]


# --- the documented state graph (architecture 5.6) --------------------------


@pytest.mark.parametrize(
    ("previous", "requested"),
    [
        (ActionState.INTENDED, ActionState.SKIPPED_IDEMPOTENT),
        (ActionState.INTENDED, ActionState.SUBMITTING),
        (ActionState.SUBMITTING, ActionState.ACKNOWLEDGED),
        (ActionState.SUBMITTING, ActionState.REJECTED),
        (ActionState.SUBMITTING, ActionState.FAILED_RETRYABLE),
        (ActionState.SUBMITTING, ActionState.FAILED_TERMINAL),
        (ActionState.SUBMITTING, ActionState.OUTCOME_UNKNOWN),
        (ActionState.FAILED_RETRYABLE, ActionState.SUBMITTING),
    ],
)
def test_documented_transitions_are_accepted(
    previous: ActionState, requested: ActionState
) -> None:
    """Exactly the transitions in architecture section 5.6 are allowed."""

    event = transition_action(previous, requested, mode=ExecutionMode.ACTIVE)
    assert event.payload["from_state"] == previous.value
    assert event.payload["to_state"] == requested.value


@pytest.mark.parametrize(
    ("previous", "requested"),
    [
        (ActionState.INTENDED, ActionState.ACKNOWLEDGED),
        (ActionState.SKIPPED_IDEMPOTENT, ActionState.SUBMITTING),
        (ActionState.ACKNOWLEDGED, ActionState.SUBMITTING),
        (ActionState.REJECTED, ActionState.SUBMITTING),
        (ActionState.FAILED_TERMINAL, ActionState.SUBMITTING),
    ],
)
def test_undocumented_transitions_are_rejected(
    previous: ActionState, requested: ActionState
) -> None:
    """Anything outside the documented graph is refused with evidence."""

    with pytest.raises(InvalidActionTransition) as raised:
        transition_action(previous, requested, mode=ExecutionMode.ACTIVE)
    assert raised.value.audit_event.reason_code == "INVALID_ACTION_TRANSITION"


def test_unknown_outcome_never_resubmits_automatically() -> None:
    """OUTCOME_UNKNOWN blocks blind resubmission (FR-013, architecture 5.6)."""

    with pytest.raises(InvalidActionTransition):
        transition_action(
            ActionState.OUTCOME_UNKNOWN,
            ActionState.SUBMITTING,
            mode=ExecutionMode.ACTIVE,
        )
    assert requires_reconciliation(ActionState.OUTCOME_UNKNOWN) is True
    assert requires_reconciliation(ActionState.FAILED_RETRYABLE) is False


def test_unknown_outcome_is_not_terminal() -> None:
    """An unknown outcome is unresolved work, not a finished action."""

    assert is_terminal_action(ActionState.OUTCOME_UNKNOWN) is False
    for state in (
        ActionState.SKIPPED_IDEMPOTENT,
        ActionState.ACKNOWLEDGED,
        ActionState.REJECTED,
        ActionState.FAILED_TERMINAL,
    ):
        assert is_terminal_action(state) is True


def test_shadow_action_may_never_submit() -> None:
    """A shadow run reaches only INTENDED or SKIPPED_IDEMPOTENT."""

    with pytest.raises(InvalidActionTransition) as raised:
        transition_action(
            ActionState.INTENDED, ActionState.SUBMITTING, mode=ExecutionMode.SHADOW
        )
    assert raised.value.audit_event.reason_code == "SHADOW_ACTION_MAY_NOT_SUBMIT"

    event = transition_action(
        ActionState.INTENDED, ActionState.SKIPPED_IDEMPOTENT, mode=ExecutionMode.SHADOW
    )
    assert event.payload["to_state"] == "SKIPPED_IDEMPOTENT"


# --- logical idempotency key (FR-013) ---------------------------------------


def test_key_is_stable_for_the_same_logical_action() -> None:
    """The same logical action always produces the same key."""

    assert build_idempotency_key(action()) == build_idempotency_key(action())


def test_key_ignores_execution_and_result_identity() -> None:
    """A retry or restart resolves to the same logical action, not a new one."""

    first = action(execution_id=uuid4(), record_processing_result_id=uuid4())
    second = action(execution_id=uuid4(), record_processing_result_id=uuid4())
    assert build_idempotency_key(first) == build_idempotency_key(second)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label_code", "LBL-0002"),
        ("action_type", "PAGE_REVERT"),
        ("desired_state", "PAGE_3"),
        ("contract_version", "aims-page-v2"),
        ("rule_version", "rules-v2"),
        ("configuration_hash", "b" * 64),
        ("source_window_start", datetime(2026, 8, 28, 6, 0, tzinfo=UTC)),
        ("source_window_end", datetime(2026, 8, 28, 8, 30, tzinfo=UTC)),
        ("key", CanonicalKey("075", "101024011793", "KGS")),
    ],
)
def test_key_changes_when_a_contributing_field_changes(
    field: str, value: object
) -> None:
    """Every documented contributor genuinely affects the key."""

    assert build_idempotency_key(action(**{field: value})) != build_idempotency_key(
        action()
    )


def test_key_is_a_sha256_digest() -> None:
    """The key is a deterministic 64-character hexadecimal digest."""

    key = build_idempotency_key(action())
    assert len(key) == 64
    assert int(key, 16) >= 0


def test_key_ignores_volatile_transport_values() -> None:
    """Transport detail must not change a logical action's identity."""

    assert build_idempotency_key(
        action(request_hash="c" * 64)
    ) == build_idempotency_key(action(request_hash=None))


def test_new_action_requires_an_ordered_utc_window() -> None:
    """The window is part of the key, so it must be well formed."""

    with pytest.raises(ValueError, match="timezone-aware"):
        action(source_window_start=datetime(2026, 8, 28, 7, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="source_window_start"):
        action(source_window_start=WINDOW_END, source_window_end=WINDOW_START)


def test_shadow_action_carries_its_mode() -> None:
    """Mode is part of the action, so persistence can enforce the shadow rule."""

    assert action(mode=ExecutionMode.SHADOW).mode is ExecutionMode.SHADOW


def test_attempt_evidence_rejects_secret_like_keys() -> None:
    """Sanitized response evidence may never carry a credential (NFR-009)."""

    from esl_service.domain.actions import ActionAttemptEvidence

    with pytest.raises(ValueError, match="forbidden evidence key"):
        ActionAttemptEvidence(
            attempt_number=1,
            started_at=WINDOW_START,
            ended_at=WINDOW_END,
            delivery_certainty=DeliveryCertainty.UNKNOWN,
            retry_class=None,
            result_code=None,
            error_class="TIMEOUT",
            response_evidence={"authorization": "Bearer x"},
        )


def test_attempt_number_starts_at_one() -> None:
    """Attempts are numbered from one so the ledger reads naturally."""

    from esl_service.domain.actions import ActionAttemptEvidence

    with pytest.raises(ValueError, match="attempt_number"):
        ActionAttemptEvidence(
            attempt_number=0,
            started_at=WINDOW_START,
            ended_at=None,
            delivery_certainty=DeliveryCertainty.UNKNOWN,
            retry_class=None,
            result_code=None,
            error_class=None,
            response_evidence={},
        )
