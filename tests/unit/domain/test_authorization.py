"""Role-based authorization of manual operations (FR-023, #26).

The role model is the owner's decision of 2026-09-02 (AD-018): two roles,
``operator`` and ``admin``. An operator may trigger, query status, retry,
replay, and reconcile; only an admin may enable or disable a schedule or
apply the cutover fallback. Nothing here touches identity *sources*: a
Principal arrives already named, and its roles come from configuration until
#28 supplies an authenticated session.
"""

import pytest

from esl_service.domain.authorization import (
    AUTHORIZATION_POLICY_VERSION,
    InvalidPrincipal,
    InvalidRoleAssignment,
    NotAuthorized,
    Operation,
    Principal,
    Role,
    authorize,
    parse_role_assignments,
    principal_for,
    require,
)

OPERATOR = Principal("alice", frozenset({Role.OPERATOR}))
ADMIN = Principal("root", frozenset({Role.ADMIN}))
NOBODY = Principal("guest", frozenset())

OPERATOR_OPERATIONS = (
    Operation.TRIGGER,
    Operation.STATUS,
    Operation.RETRY,
    Operation.REPLAY,
    Operation.RECONCILE,
)
ADMIN_OPERATIONS = (
    Operation.SCHEDULE_ENABLE,
    Operation.SCHEDULE_DISABLE,
    Operation.FALLBACK,
)


# --- the policy ---------------------------------------------------------------


@pytest.mark.parametrize("operation", OPERATOR_OPERATIONS)
def test_an_operator_may_perform_operator_operations(operation: Operation) -> None:
    decision = authorize(OPERATOR, operation)

    assert decision.allowed is True
    assert decision.required_role is Role.OPERATOR
    assert decision.policy_version == AUTHORIZATION_POLICY_VERSION


@pytest.mark.parametrize("operation", ADMIN_OPERATIONS)
def test_an_operator_may_not_perform_admin_operations(operation: Operation) -> None:
    decision = authorize(OPERATOR, operation)

    assert decision.allowed is False
    assert decision.required_role is Role.ADMIN


@pytest.mark.parametrize("operation", list(Operation))
def test_an_admin_may_perform_every_operation(operation: Operation) -> None:
    """Admin implies operator; there is no operation an admin cannot perform."""

    assert authorize(ADMIN, operation).allowed is True


@pytest.mark.parametrize("operation", list(Operation))
def test_a_principal_without_roles_may_perform_nothing(operation: Operation) -> None:
    """Being identified is not being authorized."""

    assert authorize(NOBODY, operation).allowed is False


def test_every_operation_has_a_required_role() -> None:
    """A new operation cannot be added without classifying it (FR-023)."""

    for operation in Operation:
        assert authorize(ADMIN, operation).required_role in (Role.OPERATOR, Role.ADMIN)


def test_the_decision_names_the_principal_and_operation_for_the_audit() -> None:
    decision = authorize(OPERATOR, Operation.FALLBACK)

    assert decision.identity == "alice"
    assert decision.operation is Operation.FALLBACK


def test_require_raises_carrying_the_decision() -> None:
    with pytest.raises(NotAuthorized) as caught:
        require(OPERATOR, Operation.FALLBACK)

    assert caught.value.decision.allowed is False
    assert caught.value.decision.operation is Operation.FALLBACK
    assert "fallback" in str(caught.value)
    assert "admin" in str(caught.value)


def test_require_returns_the_decision_when_allowed() -> None:
    assert require(ADMIN, Operation.FALLBACK).allowed is True


# --- principals ---------------------------------------------------------------


def test_a_principal_must_be_identified() -> None:
    with pytest.raises(InvalidPrincipal):
        Principal("   ", frozenset({Role.ADMIN}))


def test_a_principal_is_immutable() -> None:
    with pytest.raises(AttributeError):
        OPERATOR.roles = frozenset({Role.ADMIN})  # type: ignore[misc]


# --- role assignments from configuration -------------------------------------


def test_assignments_parse_identity_to_roles() -> None:
    assignments = parse_role_assignments("alice=operator;root=admin;bob=operator,admin")

    assert assignments == {
        "alice": frozenset({Role.OPERATOR}),
        "root": frozenset({Role.ADMIN}),
        "bob": frozenset({Role.OPERATOR, Role.ADMIN}),
    }


def test_assignments_tolerate_whitespace_and_a_trailing_separator() -> None:
    assignments = parse_role_assignments(" alice = operator ; root=admin ; ")

    assert set(assignments) == {"alice", "root"}


def test_empty_assignments_are_allowed() -> None:
    """A development machine may have no roles configured; nobody is authorized."""

    assert parse_role_assignments("") == {}
    assert parse_role_assignments("   ") == {}


def test_windows_account_names_match_case_insensitively() -> None:
    """Windows compares account names without case; so must the mapping."""

    assignments = parse_role_assignments("PCIT19\\Alice=admin")

    assert principal_for("pcit19\\alice", assignments).roles == frozenset({Role.ADMIN})


def test_an_unknown_role_is_refused_by_name() -> None:
    with pytest.raises(InvalidRoleAssignment) as caught:
        parse_role_assignments("alice=superuser")

    assert "superuser" in str(caught.value)


def test_a_missing_role_or_identity_is_refused() -> None:
    with pytest.raises(InvalidRoleAssignment):
        parse_role_assignments("alice=")
    with pytest.raises(InvalidRoleAssignment):
        parse_role_assignments("=admin")
    with pytest.raises(InvalidRoleAssignment):
        parse_role_assignments("alice")


def test_a_duplicate_identity_is_refused_rather_than_merged() -> None:
    """Two entries for one account is a configuration mistake, not a union."""

    with pytest.raises(InvalidRoleAssignment):
        parse_role_assignments("alice=operator;ALICE=admin")


def test_principal_for_an_unassigned_identity_has_no_roles() -> None:
    principal = principal_for("stranger", parse_role_assignments("alice=admin"))

    assert principal.identity == "stranger"
    assert principal.roles == frozenset()
