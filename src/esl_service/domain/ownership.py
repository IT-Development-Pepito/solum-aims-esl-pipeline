"""Per-scope ownership and manual-versus-scheduled priority (FR-009, FR-017).

One workflow and store form one scope, and at most one execution owns a scope
at a time. The lease that enforces this durably is persistence; the policy that
decides whether a request may take the scope is here, so it can be exercised
without a database.

The initial documented policy is **no simultaneous ownership**: a live owner is
never displaced, whatever triggered it and whatever is asking. That symmetry is
deliberate. FR-017 asks for a defined priority between scheduled and manual
operations on one scope, and the only approved answer today is that neither
preempts the other. Any preference beyond that is UNKNOWN / NEEDS-DISCOVERY and
would be an unapproved priority override, so it is not encoded here.

A refused request is rejected rather than queued, and no execution is created
for it. Two things drive that. The approved state graph allows only
``QUEUED -> RUNNING``, so a queued contender could never be cancelled or
expired; and nothing yet starts queued work, so the rows would accumulate
unseen. Rejecting matches the VERIFIED legacy behaviour, where an already
running SQL Agent job does not stack another invocation.

Every decision carries the policy version that produced it, so a later approved
policy is distinguishable in the audit trail rather than retroactively assumed.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from esl_service.domain.outcomes import TriggerType
from esl_service.domain.serialization import JSONValue

#: The policy this module implements. Recorded on every decision so an audit
#: trail written under a later approved policy is never mistaken for this one.
OWNERSHIP_POLICY_VERSION = "no-simultaneous-ownership-v1"

#: Separator between the two parts of a scope key.
SCOPE_SEPARATOR = ":"

#: Audit action names for the two ownership outcomes.
SCOPE_GRANTED = "scope.granted"
SCOPE_REJECTED = "scope.rejected"

#: Audit resource type for a scope ownership decision.
SCOPE_RESOURCE = "scope_lease"


class OwnershipOutcome(StrEnum):
    """Whether a request may take a scope."""

    GRANTED = "GRANTED"
    REJECTED = "REJECTED"


def scope_key(workflow_name: str, store_code: str) -> str:
    """Return the ownership identity of one workflow and store (FR-009).

    Neither part may be blank or contain the separator: an ambiguous key would
    let one scope masquerade as another and silently share an owner.
    """

    for name, value in (("workflow_name", workflow_name), ("store_code", store_code)):
        if not value.strip():
            raise ValueError(f"{name} must not be blank")
        if SCOPE_SEPARATOR in value:
            raise ValueError(
                f"{name} must not contain the scope separator {SCOPE_SEPARATOR!r}"
            )
    return f"{workflow_name}{SCOPE_SEPARATOR}{store_code}"


@dataclass(frozen=True)
class ScopeOwner:
    """The execution currently recorded against a scope.

    This mirrors the persisted lease without depending on it, so the policy
    stays testable without a database (NFR-011).
    """

    execution_id: UUID
    trigger_type: TriggerType
    expires_at: datetime
    released: bool

    def is_live(self, now: datetime) -> bool:
        """Return whether this owner still holds the scope.

        Expiry is inclusive, matching the persisted claim's own predicate, so
        the policy and the lease can never disagree about the same instant.
        """

        if self.released:
            return False
        return self.expires_at > now


@dataclass(frozen=True)
class OwnershipDecision:
    """One ownership decision and everything the audit trail needs to explain it."""

    outcome: OwnershipOutcome
    scope_key: str
    policy_version: str
    requested_trigger_type: TriggerType
    owner_trigger_type: TriggerType | None = None
    current_owner_execution_id: UUID | None = None

    def __post_init__(self) -> None:
        granted = self.outcome is OwnershipOutcome.GRANTED
        if granted and self.current_owner_execution_id is not None:
            raise ValueError("a granted scope has no competing owner")
        if not granted and self.current_owner_execution_id is None:
            raise ValueError("a rejected request must name the owner that held it")

    @property
    def granted(self) -> bool:
        """Whether the request may take the scope."""

        return self.outcome is OwnershipOutcome.GRANTED

    def evidence(self) -> dict[str, JSONValue]:
        """Return the sanitized evidence recorded with the audit entry."""

        owner_id = self.current_owner_execution_id
        return {
            "policy_version": self.policy_version,
            "scope_key": self.scope_key,
            "outcome": self.outcome.value,
            "requested_trigger_type": self.requested_trigger_type.value,
            "owner_trigger_type": (
                self.owner_trigger_type.value if self.owner_trigger_type else None
            ),
            "current_owner_execution_id": str(owner_id) if owner_id else None,
        }


def decide_ownership(
    scope: str,
    requested_trigger_type: TriggerType,
    *,
    current: ScopeOwner | None,
    now: datetime,
) -> OwnershipDecision:
    """Decide whether one request may take one scope (FR-009, FR-017).

    The requested and held trigger types are recorded but do not influence the
    outcome: under the initial policy a live owner is never displaced, so
    neither scheduled nor manual work preempts the other.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    if current is None or not current.is_live(now):
        return OwnershipDecision(
            outcome=OwnershipOutcome.GRANTED,
            scope_key=scope,
            policy_version=OWNERSHIP_POLICY_VERSION,
            requested_trigger_type=requested_trigger_type,
        )

    return OwnershipDecision(
        outcome=OwnershipOutcome.REJECTED,
        scope_key=scope,
        policy_version=OWNERSHIP_POLICY_VERSION,
        requested_trigger_type=requested_trigger_type,
        owner_trigger_type=current.trigger_type,
        current_owner_execution_id=current.execution_id,
    )
