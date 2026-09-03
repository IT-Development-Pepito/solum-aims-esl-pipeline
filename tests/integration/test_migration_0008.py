"""Migration ``0008_authoritative_model_gate`` (#21, AD-016 Task 8).

The gate refuses to run over data it would have to invent a value for, then
makes the schedule's configuration version mandatory, makes the *active*
schedule identity per workflow and store unique, and gives a ``RETRY_WAIT``
execution a durable due time.

The chain test drives Alembic over the dedicated test database on its own
connection and always returns it to head, so the rolled-back fixtures of the
other tests keep working.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from esl_service.domain.scheduling import ScheduleDefinition
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import ExecutionStep, WorkflowExecution
from esl_service.persistence.repository import ExecutionRepository
from tests.factories import new_execution

REVISION = "0008_authoritative_model_gate"
PREVIOUS = "0007_audit_reconciliation"
#: Marks committed rows a chain test creates, so the fixture can remove them.
COMMITTED_MARKER = "test-migration-0008"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _migrate(database_url: str, target: str, *, direction: str) -> None:
    """Run one Alembic command the way the conftest does, then restore the env."""

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


@pytest.fixture
def at_previous_revision(migrated_database_url: str) -> Iterator[str]:
    """Hold the database at 0007 for one test and bring it back to head after."""

    _migrate(migrated_database_url, PREVIOUS, direction="down")
    try:
        yield migrated_database_url
    finally:
        engine = create_engine(migrated_database_url)
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM workflow_schedule WHERE configuration_version_id IS NULL")
            )
            for statement in (
                (
                    "DELETE FROM execution_step WHERE execution_id IN "
                    "(SELECT id FROM workflow_execution WHERE rule_version = :marker)"
                ),
                "DELETE FROM workflow_execution WHERE rule_version = :marker",
                "DELETE FROM configuration_version WHERE activated_by = :marker",
            ):
                connection.execute(text(statement), {"marker": COMMITTED_MARKER})
        engine.dispose()
        _migrate(migrated_database_url, "head", direction="up")


def _insert_schedule_without_version(database_url: str) -> UUID:
    schedule_id = uuid4()
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_schedule "
                "(id, workflow_name, store_code, cron_expression, timezone, enabled, "
                " configuration_version_id, created_at, updated_at) "
                "VALUES (:id, 'esl-refresh', '099', '*/30 7-23 * * *', 'Asia/Jakarta', "
                " true, NULL, now(), now())"
            ),
            {"id": schedule_id},
        )
    engine.dispose()
    return schedule_id


def test_the_gate_refuses_a_schedule_without_a_configuration_version_by_name(
    at_previous_revision: str,
) -> None:
    """Preflight names the offending row and inserts no version of its own."""

    orphan = _insert_schedule_without_version(at_previous_revision)

    with pytest.raises(RuntimeError) as refused:
        _migrate(at_previous_revision, REVISION, direction="up")

    assert str(orphan) in str(refused.value)
    assert "esl-admin schedules create" in str(refused.value)
    engine = create_engine(at_previous_revision)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == PREVIOUS
        still_null = connection.execute(
            text("SELECT configuration_version_id FROM workflow_schedule WHERE id = :id"),
            {"id": orphan},
        ).scalar()
    engine.dispose()
    assert still_null is None


def test_the_gate_applies_and_reverts_cleanly_over_clean_data(at_previous_revision: str) -> None:
    """Upgrade adds exactly the gate; downgrade removes exactly the gate."""

    _migrate(at_previous_revision, REVISION, direction="up")
    engine = create_engine(at_previous_revision)
    inspector = inspect(engine)
    schedule_columns = {c["name"]: c for c in inspector.get_columns("workflow_schedule")}
    execution_columns = {c["name"] for c in inspector.get_columns("workflow_execution")}
    index_names = {i["name"] for i in inspector.get_indexes("workflow_schedule")}
    step_columns = {c["name"] for c in inspector.get_columns("execution_step")}
    engine.dispose()
    assert schedule_columns["configuration_version_id"]["nullable"] is False
    assert "retry_not_before" in execution_columns
    assert "uq_workflow_schedule_active_scope" in index_names
    assert "sequence" in step_columns

    _migrate(at_previous_revision, PREVIOUS, direction="down")
    engine = create_engine(at_previous_revision)
    inspector = inspect(engine)
    schedule_columns = {c["name"]: c for c in inspector.get_columns("workflow_schedule")}
    execution_columns = {c["name"] for c in inspector.get_columns("workflow_execution")}
    index_names = {i["name"] for i in inspector.get_indexes("workflow_schedule")}
    step_columns = {c["name"] for c in inspector.get_columns("execution_step")}
    engine.dispose()
    assert schedule_columns["configuration_version_id"]["nullable"] is True
    assert "retry_not_before" not in execution_columns
    assert "uq_workflow_schedule_active_scope" not in index_names
    assert "sequence" not in step_columns


def _definition(*, enabled: bool) -> ScheduleDefinition:
    return ScheduleDefinition(
        workflow_name="esl-refresh",
        store_code="084",
        cron_expression="*/30 7-23 * * *",
        timezone="Asia/Jakarta",
        enabled=enabled,
    )


def test_two_enabled_schedules_for_one_scope_are_refused_by_the_database(
    session: Session, configuration_version_id: UUID
) -> None:
    repository = LaunchRepository(session)
    repository.create_schedule(
        _definition(enabled=True), configuration_version_id=configuration_version_id,
        actor="ops.alice", reason="CHG-1",
    )

    with pytest.raises(IntegrityError) as refused:
        repository.create_schedule(
            _definition(enabled=True), configuration_version_id=configuration_version_id,
            actor="ops.alice", reason="CHG-2",
        )

    assert "uq_workflow_schedule_active_scope" in str(refused.value)


def test_a_disabled_schedule_may_keep_the_scope_of_its_enabled_replacement(
    session: Session, configuration_version_id: UUID
) -> None:
    """The identity is unique among *active* schedules; history rows stay."""

    repository = LaunchRepository(session)
    repository.create_schedule(
        _definition(enabled=False), configuration_version_id=configuration_version_id,
        actor="ops.alice", reason="CHG-1",
    )
    replacement = repository.create_schedule(
        _definition(enabled=True), configuration_version_id=configuration_version_id,
        actor="ops.alice", reason="CHG-2",
    )

    assert replacement.enabled is True


def test_runnable_executions_skip_a_retry_that_is_not_yet_due(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    due = execution_repository.create_execution(new_execution(configuration_version_id), now=now)
    not_due = execution_repository.create_execution(
        new_execution(configuration_version_id, store_code="075"), now=now
    )
    session.get_one(WorkflowExecution, due.id).retry_not_before = now - timedelta(seconds=1)
    session.get_one(WorkflowExecution, not_due.id).retry_not_before = now + timedelta(seconds=30)
    session.flush()

    assert execution_repository.runnable_executions(limit=10, now=now) == [due.id]
    assert execution_repository.runnable_executions(limit=10, now=now + timedelta(seconds=31)) == [
        due.id,
        not_due.id,
    ]


def test_step_history_follows_start_order_even_when_timestamps_tie(
    session: Session, execution_repository: ExecutionRepository, configuration_version_id: UUID
) -> None:
    """A coarse host clock stamps a whole run's steps identically; order must not depend on it."""

    execution = execution_repository.create_execution(new_execution(configuration_version_id))
    names = ["discover", "read-warehouse", "read-store"]
    for name in names:
        execution_repository.start_step(execution.id, name)
    tie = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    for step in session.scalars(select(ExecutionStep).where(ExecutionStep.execution_id == execution.id)):
        step.started_at = tie
    session.flush()
    session.expire_all()

    history = execution_repository.step_history(execution.id)

    assert [step.step_name for step in history] == names
    sequences = [step.sequence for step in history]
    assert sequences == sorted(sequences) and len(set(sequences)) == 3


def _insert_steps_with_reversed_timestamps(database_url: str) -> list[UUID]:
    """Commit one execution with three steps whose insertion and start orders disagree."""

    engine = create_engine(database_url)
    version_id, execution_id = uuid4(), uuid4()
    step_ids = [uuid4() for _ in range(3)]
    base = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO configuration_version "
                "(id, environment, schema_version, content_hash, sanitized_snapshot, activated_by) "
                "VALUES (:id, 'test', 1, :hash, '{}'::jsonb, :marker)"
            ),
            {"id": version_id, "hash": "0" * 64, "marker": COMMITTED_MARKER},
        )
        connection.execute(
            text(
                "INSERT INTO workflow_execution (id, workflow_name, store_code, trigger_type, mode, "
                "correlation_id, source_window_start, source_window_end, configuration_version_id, "
                "rule_version, started_at, status) VALUES (:id, 'esl-refresh', '084', 'MANUAL', "
                "'SHADOW', :corr, :start, :end, :version, :marker, :start, 'RUNNING')"
            ),
            {"id": execution_id, "corr": uuid4(), "start": base, "end": base + timedelta(minutes=30),
             "version": version_id, "marker": COMMITTED_MARKER},
        )
        # Inserted newest-first, so heap order and start order disagree.
        for offset, step_id in zip((3, 2, 1), step_ids, strict=True):
            connection.execute(
                text(
                    "INSERT INTO execution_step (id, execution_id, step_name, attempt, outcome, started_at) "
                    "VALUES (:id, :execution, :name, 1, 'SUCCEEDED', :started)"
                ),
                {"id": step_id, "execution": execution_id, "name": f"step-{offset}",
                 "started": base + timedelta(seconds=offset)},
            )
    engine.dispose()
    return list(reversed(step_ids))  # by start time: step-1, step-2, step-3


def test_the_gate_numbers_existing_steps_by_start_time_not_heap_order(
    at_previous_revision: str,
) -> None:
    by_start = _insert_steps_with_reversed_timestamps(at_previous_revision)

    _migrate(at_previous_revision, REVISION, direction="up")

    engine = create_engine(at_previous_revision)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, sequence FROM execution_step WHERE id = ANY(:ids) ORDER BY sequence"),
            {"ids": by_start},
        ).all()
    engine.dispose()
    assert [row.id for row in rows] == by_start
    assert [row.sequence for row in rows] == sorted(row.sequence for row in rows)
