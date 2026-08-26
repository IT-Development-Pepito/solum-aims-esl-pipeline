"""Integration coverage for durable workflow-state persistence (FR-017)."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from esl_service.persistence.repository import ExecutionRepository


@pytest.fixture
def repository() -> Iterator[ExecutionRepository]:
    """Provide a real repository whose state is rolled back after the test."""

    database_url = os.environ["ESL_TEST_DATABASE_URL"]
    engine = create_engine(database_url)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield ExecutionRepository(session)
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()


def test_only_one_execution_claims_store_scope(
    repository: ExecutionRepository,
) -> None:
    """An overlapping execution cannot claim an already leased store scope."""

    first = repository.create_execution("sku-shadow", "084", "2026-08-25T07:00:00Z")
    assert repository.claim_scope(first.id, "sku-shadow:084") is True

    second = repository.create_execution("sku-shadow", "084", "2026-08-25T07:00:00Z")
    assert repository.claim_scope(second.id, "sku-shadow:084") is False
