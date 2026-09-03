"""Committed rows in the dedicated test database, and their removal.

The rolled-back ``session`` fixture cannot serve a scenario in which a second
process, or a second engine standing in for a restarted server, must see what
the first one wrote. These helpers commit through their own engine and purge
exactly what one execution produced, in foreign-key order, so the database
is left as it was found.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

FORBIDDEN_DATABASE_NAMES = frozenset({"postgres", "template0", "template1"})


def test_database_url() -> str:
    """The dedicated test database URL, or a skip; never a system or production database."""

    url = os.environ.get("ESL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ESL_TEST_DATABASE_URL is required for committed-state recovery scenarios")
    name = make_url(url).database
    if not name or name in FORBIDDEN_DATABASE_NAMES:
        raise RuntimeError("ESL_TEST_DATABASE_URL must name a dedicated database")
    if name == os.environ.get("ESL_PRODUCTION_DATABASE_NAME"):
        raise RuntimeError("ESL_TEST_DATABASE_URL must not target the configured production database")
    return url


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def committed_engine() -> Iterator[Engine]:
    engine = create_engine(test_database_url())
    try:
        yield engine
    finally:
        engine.dispose()


def commit_configuration_version(engine: Engine, marker: str) -> UUID:
    """Commit one configuration version the scenario's executions can reference."""

    version_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO configuration_version "
                "(id, environment, schema_version, content_hash, sanitized_snapshot, activated_by) "
                "VALUES (:id, 'test', 1, :hash, '{}'::jsonb, :marker)"
            ),
            {"id": version_id, "hash": uuid4().hex + uuid4().hex, "marker": marker},
        )
    return version_id


_PURGE_STATEMENTS = (
    # deepest evidence first; every statement is bound to one execution
    (
        "DELETE FROM reconciliation_exception WHERE report_id IN (SELECT id FROM "
        "reconciliation_report WHERE execution_id = :id)"
    ),
    (
        "DELETE FROM action_attempt WHERE action_id IN (SELECT id FROM record_action WHERE "
        "execution_id = :id)"
    ),
    (
        "DELETE FROM record_issue WHERE result_id IN (SELECT id FROM record_processing_result "
        "WHERE execution_id = :id)"
    ),
    "DELETE FROM record_action WHERE execution_id = :id",
    (
        "UPDATE promotion_evaluation SET selected_candidate_id = NULL WHERE "
        "canonical_record_snapshot_id IN (SELECT s.id FROM canonical_record_snapshot s JOIN "
        "snapshot_set ss ON ss.id = s.snapshot_set_id WHERE ss.execution_id = :id)"
    ),
    (
        "DELETE FROM promotion_candidate_snapshot WHERE evaluation_id IN (SELECT e.id FROM "
        "promotion_evaluation e JOIN canonical_record_snapshot s ON s.id = "
        "e.canonical_record_snapshot_id JOIN snapshot_set ss ON ss.id = s.snapshot_set_id "
        "WHERE ss.execution_id = :id)"
    ),
    (
        "DELETE FROM promotion_evaluation WHERE canonical_record_snapshot_id IN (SELECT s.id "
        "FROM canonical_record_snapshot s JOIN snapshot_set ss ON ss.id = s.snapshot_set_id "
        "WHERE ss.execution_id = :id)"
    ),
    "DELETE FROM record_processing_result WHERE execution_id = :id",
    "DELETE FROM record_difference WHERE execution_id = :id",
    (
        "DELETE FROM canonical_record_snapshot WHERE snapshot_set_id IN (SELECT id FROM "
        "snapshot_set WHERE execution_id = :id)"
    ),
    "DELETE FROM snapshot_set WHERE execution_id = :id",
    (
        "DELETE FROM execution_checkpoint WHERE step_id IN (SELECT id FROM execution_step "
        "WHERE execution_id = :id)"
    ),
    "DELETE FROM execution_step WHERE execution_id = :id",
    "DELETE FROM execution_event WHERE execution_id = :id",
    "DELETE FROM scope_lease WHERE execution_id = :id",
    "DELETE FROM reconciliation_report WHERE execution_id = :id",
    "DELETE FROM audit_entry WHERE execution_id = :id",
    "DELETE FROM workflow_execution WHERE id = :id",
)


def purge_execution(engine: Engine, execution_id: UUID) -> None:
    """Remove everything one execution wrote, in foreign-key order."""

    with engine.begin() as connection:
        for statement in _PURGE_STATEMENTS:
            connection.execute(text(statement), {"id": execution_id})


def purge_configuration_versions(engine: Engine, marker: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM audit_entry WHERE configuration_version_id IN "
                 "(SELECT id FROM configuration_version WHERE activated_by = :marker)"),
            {"marker": marker},
        )
        connection.execute(
            text("DELETE FROM configuration_version WHERE activated_by = :marker"),
            {"marker": marker},
        )
