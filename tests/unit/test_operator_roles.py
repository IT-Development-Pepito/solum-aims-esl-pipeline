"""Role assignments come from configuration until #28 authenticates (FR-023).

``ESL_OPERATOR_ROLES`` maps an account name to roles. It is not a secret and
it is part of the sanitized configuration snapshot, so an execution records
who was authorized under it. The runtime resolves the current Windows account
to a Principal; the domain decides what that Principal may do.
"""

import pytest

from esl_service.config import (
    Settings,
    build_role_assignments,
    sanitized_configuration_snapshot,
    validate_startup_configuration,
)
from esl_service.domain.authorization import Role
from esl_service.runtime.principals import current_principal

BASE = {
    "environment": "development",
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl_pipeline_dev",
    "internal_host": "127.0.0.1",
}


def settings(**overrides: object) -> Settings:
    return Settings.model_validate({**BASE, **overrides})


def test_roles_default_to_nobody_authorized() -> None:
    assert build_role_assignments(settings()) == {}


def test_roles_are_parsed_from_the_setting() -> None:
    assignments = build_role_assignments(
        settings(operator_roles="PCIT19\\alice=operator;PCIT19\\root=admin")
    )

    assert assignments["pcit19\\alice"] == frozenset({Role.OPERATOR})
    assert assignments["pcit19\\root"] == frozenset({Role.ADMIN})


def test_a_malformed_mapping_is_a_startup_configuration_problem() -> None:
    """The service must refuse readiness rather than run with nobody authorized."""

    validated, problems = validate_startup_configuration(
        {**BASE, "operator_roles": "alice=superuser"}
    )

    assert validated is None
    (problem,) = problems
    assert problem.key == "operator_roles"
    assert "superuser" in problem.message


def test_role_assignments_are_part_of_the_configuration_snapshot() -> None:
    snapshot = sanitized_configuration_snapshot(settings(operator_roles="alice=admin"))

    assert snapshot["operator_roles"] == "alice=admin"


def test_the_current_principal_is_the_running_account_with_its_configured_roles() -> None:
    principal = current_principal(
        settings(operator_roles="PCIT19\\Alice=operator"),
        account_name=lambda: "pcit19\\alice",
    )

    assert principal.identity == "pcit19\\alice"
    assert principal.roles == frozenset({Role.OPERATOR})


def test_an_unassigned_account_becomes_a_principal_with_no_roles() -> None:
    principal = current_principal(settings(), account_name=lambda: "pcit19\\stranger")

    assert principal.roles == frozenset()


def test_a_blank_account_name_is_refused() -> None:
    with pytest.raises(ValueError, match="identity"):
        current_principal(settings(), account_name=lambda: "")
