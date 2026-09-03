"""The models package split must preserve every existing import path.

Requirement: AD-016 Task 2 replaces ``persistence/models.py`` with a package and
must keep ``esl_service.persistence.models`` importable exactly as before so no
caller, migration, or Alembic autogenerate target breaks.
"""

from sqlalchemy.orm import DeclarativeBase

from esl_service.persistence.models import (
    Base,
    CanonicalRecordSnapshot,
    ConfigurationVersion,
    ExecutionEvent,
    RecordAction,
    RecordDifference,
    ScopeLease,
    SnapshotSet,
    StoreConfiguration,
    WorkflowExecution,
    WorkflowSchedule,
)

BASELINE_TABLES = frozenset(
    {
        "workflow_execution",
        "scope_lease",
        "execution_event",
        "record_action",
        "workflow_schedule",
    }
)

TASK_2_TABLES = frozenset(
    {
        "store_configuration",
        "configuration_version",
        "snapshot_set",
        "canonical_record_snapshot",
        "record_difference",
    }
)


def test_existing_model_imports_are_preserved() -> None:
    """The five baseline classes still import from the unchanged module path."""

    assert issubclass(Base, DeclarativeBase)
    assert WorkflowExecution.__tablename__ == "workflow_execution"
    assert ScopeLease.__tablename__ == "scope_lease"
    assert ExecutionEvent.__tablename__ == "execution_event"
    assert RecordAction.__tablename__ == "record_action"
    assert WorkflowSchedule.__tablename__ == "workflow_schedule"


def test_new_model_imports_are_available() -> None:
    """Configuration and canonical evidence classes share the same import path."""

    assert StoreConfiguration.__tablename__ == "store_configuration"
    assert ConfigurationVersion.__tablename__ == "configuration_version"
    assert SnapshotSet.__tablename__ == "snapshot_set"
    assert CanonicalRecordSnapshot.__tablename__ == "canonical_record_snapshot"
    assert RecordDifference.__tablename__ == "record_difference"


def test_metadata_contains_every_mapped_table() -> None:
    """Base.metadata stays complete so migrations and autogenerate see all tables."""

    assert BASELINE_TABLES | TASK_2_TABLES <= set(Base.metadata.tables)


def test_workflow_schedule_requires_a_configuration_version_link() -> None:
    """Since 0008 a schedule cannot exist without the configuration version it runs under."""

    columns = WorkflowSchedule.__table__.columns
    assert "configuration_version_id" in columns
    assert columns["configuration_version_id"].nullable is False
    assert "updated_at" in columns
