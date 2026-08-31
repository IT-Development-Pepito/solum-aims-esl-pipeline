"""Integration coverage for durable workflow-state persistence (FR-017).

Uses the shared migrated-database fixtures in ``conftest.py`` so this module
exercises the same schema revision as the rest of the integration suite.
"""

from uuid import UUID

from esl_service.persistence.repository import ExecutionRepository
from tests.factories import new_execution


def test_only_one_execution_claims_store_scope(
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """An overlapping execution cannot claim an already leased store scope."""

    first = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    assert execution_repository.claim_scope(first.id, "sku-shadow:084") is True

    second = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    assert execution_repository.claim_scope(second.id, "sku-shadow:084") is False
