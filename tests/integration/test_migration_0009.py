"""Migration ``0009_relax_action_evidence_links`` (#64).

Retaining the audit core pinned the detailed rows beneath it: an action's
processing result and a result's canonical snapshot were both ``NOT NULL``
with ``RESTRICT``, so a purge could never remove canonical snapshots, the
largest class by volume. The gate makes exactly those two links optional
and nothing else, so a purge can null them and delete what they pinned.

The chain test drives Alembic on its own connection and always returns the
database to head, as ``test_migration_0008`` does.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

REVISION = "0009_relax_action_evidence_links"
PREVIOUS = "0008_authoritative_model_gate"
#: Marks committed rows this module creates, so they can always be removed.
COMMITTED_MARKER = "test-migration-0009"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _migrate(database_url: str, target: str, *, direction: str) -> None:
    previous = os.environ.get("ESL_DATABASE_URL")
    os.environ["ESL_DATABASE_URL"] = database_url
    try:
        config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_REPOSITORY_ROOT / "alembic"))
        if direction == "up":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("ESL_DATABASE_URL", None)
        else:
            os.environ["ESL_DATABASE_URL"] = previous


def _nullable(database_url: str) -> dict[str, bool]:
    engine = create_engine(database_url)
    inspector = inspect(engine)
    columns = {
        "record_action.record_processing_result_id": {
            c["name"]: c for c in inspector.get_columns("record_action")
        }["record_processing_result_id"]["nullable"],
        "record_processing_result.canonical_record_snapshot_id": {
            c["name"]: c for c in inspector.get_columns("record_processing_result")
        }["canonical_record_snapshot_id"]["nullable"],
    }
    engine.dispose()
    return columns


@pytest.fixture
def at_previous_revision(migrated_database_url: str) -> Iterator[str]:
    """Hold the database at 0008 for one test and bring it back to head after."""

    _migrate(migrated_database_url, PREVIOUS, direction="down")
    try:
        yield migrated_database_url
    finally:
        _purge_marked(migrated_database_url)
        _migrate(migrated_database_url, "head", direction="up")


def test_the_gate_makes_exactly_the_two_pinned_links_optional(
    at_previous_revision: str,
) -> None:
    """Both are NOT NULL at 0008, both optional at 0009, both restored on downgrade."""

    assert _nullable(at_previous_revision) == {
        "record_action.record_processing_result_id": False,
        "record_processing_result.canonical_record_snapshot_id": False,
    }

    _migrate(at_previous_revision, REVISION, direction="up")

    assert _nullable(at_previous_revision) == {
        "record_action.record_processing_result_id": True,
        "record_processing_result.canonical_record_snapshot_id": True,
    }

    _migrate(at_previous_revision, PREVIOUS, direction="down")

    assert _nullable(at_previous_revision) == {
        "record_action.record_processing_result_id": False,
        "record_processing_result.canonical_record_snapshot_id": False,
    }


def test_rows_written_before_the_gate_keep_their_links(at_previous_revision: str) -> None:
    """Prior-schema coverage: relaxing a constraint changes no existing row."""

    action_id, result_id, snapshot_id = _insert_linked_rows(at_previous_revision)

    _migrate(at_previous_revision, REVISION, direction="up")

    engine = create_engine(at_previous_revision)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT record_processing_result_id FROM record_action WHERE id = :id"),
            {"id": action_id},
        ).scalar() == result_id
        assert connection.execute(
            text(
                "SELECT canonical_record_snapshot_id FROM record_processing_result "
                "WHERE id = :id"
            ),
            {"id": result_id},
        ).scalar() == snapshot_id
    engine.dispose()


def _insert_linked_rows(database_url: str) -> tuple[UUID, UUID, UUID]:
    """Commit one execution, snapshot, result, and action, all links set."""

    version_id, execution_id = uuid4(), uuid4()
    set_id, snapshot_id, result_id, action_id = uuid4(), uuid4(), uuid4(), uuid4()
    start = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO configuration_version (id, environment, schema_version, "
                "content_hash, sanitized_snapshot, activated_by) "
                "VALUES (:id, 'test', 1, :hash, '{}'::jsonb, :marker)"
            ),
            {"id": version_id, "hash": uuid4().hex + uuid4().hex, "marker": COMMITTED_MARKER},
        )
        c.execute(
            text(
                "INSERT INTO workflow_execution (id, workflow_name, store_code, trigger_type, "
                "mode, correlation_id, source_window_start, source_window_end, "
                "configuration_version_id, rule_version, started_at, status) "
                "VALUES (:id, 'esl-refresh', '084', 'MANUAL', 'SHADOW', :corr, :start, :end, "
                ":version, :marker, :start, 'SUCCEEDED')"
            ),
            {"id": execution_id, "corr": uuid4(), "start": start,
             "end": start + timedelta(minutes=30), "version": version_id, "marker": COMMITTED_MARKER},
        )
        c.execute(
            text(
                "INSERT INTO snapshot_set (id, execution_id, representation_kind, adapter_name, "
                "source_watermark, canonical_schema_version, record_count) "
                "VALUES (:id, :execution, 'SOURCE_EXPECTED', 'test', 'w', 'canonical-v1', 1)"
            ),
            {"id": set_id, "execution": execution_id},
        )
        c.execute(
            text(
                "INSERT INTO canonical_record_snapshot (id, snapshot_set_id, store_code, "
                "item_code, selling_uom, canonical_schema_version, canonical_hash, payload) "
                "VALUES (:id, :set_id, '084', 'A', 'KGS', 'canonical-v1', :hash, '{}'::jsonb)"
            ),
            {"id": snapshot_id, "set_id": set_id, "hash": "a" * 64},
        )
        c.execute(
            text(
                "INSERT INTO record_processing_result (id, execution_id, "
                "canonical_record_snapshot_id, store_code, item_code, selling_uom, "
                "validation_status, eligibility_status, action_decision, processing_status) "
                "VALUES (:id, :execution, :snapshot, '084', 'A', 'KGS', 'VALID', "
                "'ELIGIBLE', 'NONE', 'UNCHANGED')"
            ),
            {"id": result_id, "execution": execution_id, "snapshot": snapshot_id},
        )
        c.execute(
            text(
                "INSERT INTO record_action (id, execution_id, record_processing_result_id, "
                "store_code, item_code, selling_uom, record_key, label_code, action_type, "
                "desired_page, desired_state, idempotency_key, request_hash, state, mode, "
                "contract_version, rule_version, configuration_hash, source_window_start, "
                "source_window_end, payload) "
                "VALUES (:id, :execution, :result, '084', 'A', 'KGS', '084:A:KGS', 'LBL-1', "
                "'PAGE_CHANGE', 2, 'PAGE_2', :key, :hash, 'INTENDED', 'SHADOW', 'aims-page-v1', "
                "'rules-v1', :hash, :start, :end, '{}'::jsonb)"
            ),
            {"id": action_id, "execution": execution_id, "result": result_id,
             "key": "k" * 64, "hash": "b" * 64, "start": start, "end": start + timedelta(minutes=30)},
        )
    engine.dispose()
    return action_id, result_id, snapshot_id


def _purge_marked(database_url: str) -> None:
    """Remove the committed rows, newest dependency first."""

    engine = create_engine(database_url)
    with engine.begin() as c:
        executions = (
            "SELECT id FROM workflow_execution WHERE rule_version = :marker "
            "OR configuration_version_id IN "
            "(SELECT id FROM configuration_version WHERE activated_by = :marker)"
        )
        for statement in (
            f"DELETE FROM record_action WHERE execution_id IN ({executions})",
            f"DELETE FROM record_processing_result WHERE execution_id IN ({executions})",
            (
                "DELETE FROM canonical_record_snapshot WHERE snapshot_set_id IN "
                f"(SELECT id FROM snapshot_set WHERE execution_id IN ({executions}))"
            ),
            f"DELETE FROM snapshot_set WHERE execution_id IN ({executions})",
            f"DELETE FROM workflow_execution WHERE id IN ({executions})",
            "DELETE FROM configuration_version WHERE activated_by = :marker",
        ):
            c.execute(text(statement), {"marker": COMMITTED_MARKER})
    engine.dispose()
