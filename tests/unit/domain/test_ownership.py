"""Per-scope ownership and manual-versus-scheduled priority (FR-009, FR-017).

The initial documented policy is no simultaneous ownership. Nothing preempts a
live owner, so the trigger type of either party never changes the decision. Any
preference between a manual and a scheduled operation on the same scope remains
UNKNOWN / NEEDS-DISCOVERY, and this module must not invent one.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from esl_service.domain.outcomes import TriggerType
from esl_service.domain.ownership import (
    OWNERSHIP_POLICY_VERSION,
    OwnershipOutcome,
    ScopeOwner,
    decide_ownership,
    scope_key,
)

NOW = datetime(2026, 8, 31, 7, 30, tzinfo=UTC)
SCOPE = "esl-refresh:084"


def owner(**overrides: object) -> ScopeOwner:
    """Build a live owner, overriding only what a test needs."""

    values: dict[str, object] = {
        "execution_id": uuid4(),
        "trigger_type": TriggerType.SCHEDULED,
        "expires_at": NOW + timedelta(minutes=15),
        "released": False,
    }
    values.update(overrides)
    return ScopeOwner(**values)  # type: ignore[arg-type]


# --- one scope identity, derived not improvised ----------------------------


def test_a_scope_is_identified_by_workflow_and_store() -> None:
    """Ownership is per workflow and store, so the key names exactly those."""

    assert scope_key("esl-refresh", "084") == SCOPE


def test_different_stores_are_different_scopes() -> None:
    """Two stores never contend with each other."""

    assert scope_key("esl-refresh", "084") != scope_key("esl-refresh", "075")


def test_different_workflows_are_different_scopes() -> None:
    """Two workflows on one store never contend with each other."""

    assert scope_key("sku-shadow", "084") != scope_key("esl-refresh", "084")


@pytest.mark.parametrize(
    ("workflow_name", "store_code"), [("", "084"), ("esl-refresh", "  ")]
)
def test_a_scope_key_needs_both_parts(workflow_name: str, store_code: str) -> None:
    """A half-identified scope would collide with another, so it is refused."""

    with pytest.raises(ValueError):
        scope_key(workflow_name, store_code)


def test_a_workflow_name_containing_the_separator_is_refused() -> None:
    """Ambiguous keys would let one scope masquerade as another."""

    with pytest.raises(ValueError, match="separator"):
        scope_key("esl:refresh", "084")


# --- an unowned scope is granted ------------------------------------------


def test_an_unowned_scope_is_granted() -> None:
    """Nothing owns the scope, so the request takes it."""

    decision = decide_ownership(SCOPE, TriggerType.SCHEDULED, current=None, now=NOW)

    assert decision.outcome is OwnershipOutcome.GRANTED
    assert decision.current_owner_execution_id is None


def test_a_released_scope_is_granted() -> None:
    """A finished owner released the scope, so the next request may take it."""

    decision = decide_ownership(
        SCOPE, TriggerType.MANUAL, current=owner(released=True), now=NOW
    )

    assert decision.outcome is OwnershipOutcome.GRANTED


def test_an_expired_scope_is_granted() -> None:
    """An owner that stopped heartbeating no longer holds the scope."""

    expired = owner(expires_at=NOW - timedelta(seconds=1))

    decision = decide_ownership(SCOPE, TriggerType.MANUAL, current=expired, now=NOW)

    assert decision.outcome is OwnershipOutcome.GRANTED


def test_a_lease_expiring_exactly_now_is_no_longer_live() -> None:
    """Expiry is inclusive, matching the persisted claim's own predicate."""

    decision = decide_ownership(
        SCOPE, TriggerType.MANUAL, current=owner(expires_at=NOW), now=NOW
    )

    assert decision.outcome is OwnershipOutcome.GRANTED


# --- a live owner is never displaced (FR-009, FR-017) ---------------------


@pytest.mark.parametrize("requested", list(TriggerType))
@pytest.mark.parametrize("held_by", list(TriggerType))
def test_no_trigger_type_ever_displaces_a_live_owner(
    requested: TriggerType, held_by: TriggerType
) -> None:
    """The initial policy is no simultaneous ownership, for every combination.

    Iterating both trigger types is the point: if any pair were allowed to
    preempt, this would assert a priority that no document approves.
    """

    decision = decide_ownership(
        SCOPE, requested, current=owner(trigger_type=held_by), now=NOW
    )

    assert decision.outcome is OwnershipOutcome.REJECTED


def test_a_manual_request_does_not_displace_a_scheduled_owner() -> None:
    """Named explicitly because FR-017 is about exactly this pair."""

    held = owner(trigger_type=TriggerType.SCHEDULED)

    decision = decide_ownership(SCOPE, TriggerType.MANUAL, current=held, now=NOW)

    assert decision.outcome is OwnershipOutcome.REJECTED
    assert decision.current_owner_execution_id == held.execution_id


def test_a_scheduled_request_does_not_displace_a_manual_owner() -> None:
    """The converse holds too, so the policy is symmetric rather than a priority."""

    held = owner(trigger_type=TriggerType.MANUAL)

    decision = decide_ownership(SCOPE, TriggerType.SCHEDULED, current=held, now=NOW)

    assert decision.outcome is OwnershipOutcome.REJECTED
    assert decision.current_owner_execution_id == held.execution_id


# --- the decision explains itself (FR-009, FR-022) ------------------------


def test_a_rejection_names_the_scope_owner_and_both_trigger_types() -> None:
    """The audit trail must answer who held the scope and what was refused."""

    held = owner(trigger_type=TriggerType.SCHEDULED)

    decision = decide_ownership(SCOPE, TriggerType.MANUAL, current=held, now=NOW)

    assert decision.scope_key == SCOPE
    assert decision.requested_trigger_type is TriggerType.MANUAL
    assert decision.owner_trigger_type is TriggerType.SCHEDULED
    assert decision.current_owner_execution_id == held.execution_id


def test_every_decision_records_the_policy_it_applied() -> None:
    """A later approved policy must be distinguishable from this one."""

    granted = decide_ownership(SCOPE, TriggerType.MANUAL, current=None, now=NOW)
    rejected = decide_ownership(SCOPE, TriggerType.MANUAL, current=owner(), now=NOW)

    assert granted.policy_version == OWNERSHIP_POLICY_VERSION
    assert rejected.policy_version == OWNERSHIP_POLICY_VERSION


def test_a_granted_decision_names_no_owner_trigger_type() -> None:
    """Nothing held the scope, so no owner detail may be fabricated."""

    decision = decide_ownership(SCOPE, TriggerType.MANUAL, current=None, now=NOW)

    assert decision.owner_trigger_type is None


def test_the_decision_is_audit_ready_evidence() -> None:
    """The decision serializes to the sanitized evidence the audit stores."""

    held = owner(trigger_type=TriggerType.SCHEDULED)
    decision = decide_ownership(SCOPE, TriggerType.MANUAL, current=held, now=NOW)

    assert decision.evidence() == {
        "policy_version": OWNERSHIP_POLICY_VERSION,
        "scope_key": SCOPE,
        "outcome": OwnershipOutcome.REJECTED.value,
        "requested_trigger_type": TriggerType.MANUAL.value,
        "owner_trigger_type": TriggerType.SCHEDULED.value,
        "current_owner_execution_id": str(held.execution_id),
    }


def test_an_instant_without_a_timezone_is_refused() -> None:
    """Liveness compares instants, so a naive one cannot be evaluated."""

    with pytest.raises(ValueError, match="timezone-aware"):
        decide_ownership(
            SCOPE,
            TriggerType.MANUAL,
            current=owner(),
            now=datetime(2026, 8, 31, 7, 30),  # noqa: DTZ001
        )
