"""Role-based authorization of manual operations (FR-023, AD-018).

Two roles, decided by the owner on 2026-09-02 and recorded as AD-018:

- ``operator`` may trigger a run, query status, retry, replay, and request
  reconciliation;
- ``admin`` may do everything an operator may, and additionally enable or
  disable a schedule and apply the cutover fallback.

The model is deliberately small. No document defines a finer vocabulary, and
inventing one would be a business rule; a later approved model is
distinguishable in the audit trail by ``AUTHORIZATION_POLICY_VERSION``.

This module decides only *whether* a named principal may perform an
operation. Where the principal's identity comes from -- a Windows account
name today, an authenticated session once #28 lands -- and where its roles
come from -- configuration today -- are runtime concerns and stay out of the
domain. A principal that is identified but holds no role may do nothing:
being known is not being authorized.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

#: Recorded on every decision so a later policy is distinguishable.
AUTHORIZATION_POLICY_VERSION = "two-role-v1"

#: Audit vocabulary for refusals and the operations that have no other trail.
OPERATION_REFUSED = "operation.refused"
AUTHORIZATION_RESOURCE = "authorization"
RECONCILIATION_REQUESTED = "reconciliation.requested"
FALLBACK_APPLIED = "fallback.applied"
FALLBACK_RESOURCE = "cutover_fallback"


class Role(StrEnum):
    """The two approved roles (AD-018). ``ADMIN`` implies ``OPERATOR``."""

    OPERATOR = "operator"
    ADMIN = "admin"


class Operation(StrEnum):
    """The manual operations FR-023 lists, each classified below."""

    TRIGGER = "trigger"
    STATUS = "status"
    RETRY = "retry"
    REPLAY = "replay"
    SCHEDULE_ENABLE = "schedule_enable"
    SCHEDULE_DISABLE = "schedule_disable"
    RECONCILE = "reconcile"
    FALLBACK = "fallback"


#: The least role that may perform each operation. Every operation must
#: appear here; ``authorize`` fails loudly for one that does not.
REQUIRED_ROLE: Mapping[Operation, Role] = MappingProxyType(
    {
        Operation.TRIGGER: Role.OPERATOR,
        Operation.STATUS: Role.OPERATOR,
        Operation.RETRY: Role.OPERATOR,
        Operation.REPLAY: Role.OPERATOR,
        Operation.RECONCILE: Role.OPERATOR,
        Operation.SCHEDULE_ENABLE: Role.ADMIN,
        Operation.SCHEDULE_DISABLE: Role.ADMIN,
        Operation.FALLBACK: Role.ADMIN,
    }
)


class InvalidPrincipal(ValueError):
    """Raised when a principal has no usable identity."""


class InvalidRoleAssignment(ValueError):
    """Raised when a configured identity-to-role mapping cannot be read."""


@dataclass(frozen=True)
class Principal:
    """Who is asking: a named identity and the roles it holds."""

    identity: str
    roles: frozenset[Role]

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise InvalidPrincipal("a principal must carry a non-blank identity")


@dataclass(frozen=True)
class AuthorizationDecision:
    """Whether one principal may perform one operation, and under which rule."""

    allowed: bool
    operation: Operation
    identity: str
    required_role: Role
    policy_version: str = AUTHORIZATION_POLICY_VERSION


class NotAuthorized(PermissionError):
    """Raised when an operation is refused; carries the decision for the audit."""

    def __init__(self, decision: AuthorizationDecision) -> None:
        self.decision = decision
        super().__init__(
            f"{decision.identity!r} may not perform {decision.operation.value}: "
            f"role {decision.required_role.value} is required"
        )


def _satisfies(held: frozenset[Role], required: Role) -> bool:
    if Role.ADMIN in held:
        return True
    return required is Role.OPERATOR and Role.OPERATOR in held


def authorize(principal: Principal, operation: Operation) -> AuthorizationDecision:
    """Decide whether ``principal`` may perform ``operation`` (FR-023)."""

    required = REQUIRED_ROLE[operation]
    return AuthorizationDecision(
        allowed=_satisfies(principal.roles, required),
        operation=operation,
        identity=principal.identity,
        required_role=required,
    )


def require(principal: Principal, operation: Operation) -> AuthorizationDecision:
    """Return the decision when allowed, otherwise raise ``NotAuthorized``."""

    decision = authorize(principal, operation)
    if not decision.allowed:
        raise NotAuthorized(decision)
    return decision


# --- role assignments -------------------------------------------------------


def _normalised(identity: str) -> str:
    """Windows compares account names without regard to case; so do we."""

    return identity.strip().casefold()


def parse_role_assignments(text: str) -> dict[str, frozenset[Role]]:
    """Parse ``identity=role[,role];identity=role`` into a lookup table.

    Identities are normalised with ``casefold`` so ``PCIT19\\Alice`` and
    ``pcit19\\alice`` are one account. An unknown role, a missing side, or a
    duplicate identity is refused rather than guessed: the mapping is what
    stands between an account and an operation.
    """

    assignments: dict[str, frozenset[Role]] = {}
    for raw_entry in text.split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        identity_part, separator, roles_part = entry.partition("=")
        identity = _normalised(identity_part)
        if not separator or not identity or not roles_part.strip():
            raise InvalidRoleAssignment(
                f"entry {entry!r} must have the form identity=role[,role]"
            )
        if identity in assignments:
            raise InvalidRoleAssignment(f"identity {identity!r} is assigned more than once")
        roles: set[Role] = set()
        for raw_role in roles_part.split(","):
            name = raw_role.strip().casefold()
            try:
                roles.add(Role(name))
            except ValueError:
                raise InvalidRoleAssignment(
                    f"unknown role {name!r} for {identity!r}; "
                    f"valid roles are {', '.join(role.value for role in Role)}"
                ) from None
        assignments[identity] = frozenset(roles)
    return assignments


def principal_for(identity: str, assignments: Mapping[str, frozenset[Role]]) -> Principal:
    """Return the principal for ``identity`` under ``assignments``.

    An identity that is not assigned becomes a principal with no roles, so
    every operation is refused and the refusal is audited under its name.
    """

    return Principal(identity=identity, roles=assignments.get(_normalised(identity), frozenset()))
