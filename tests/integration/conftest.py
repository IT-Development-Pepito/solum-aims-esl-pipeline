"""Shared PostgreSQL integration fixtures for the service-owned state schema.

These fixtures never target a production database. They require an explicitly
configured, dedicated non-production database, migrate it to the revision under
test, and roll back every row a test creates.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from alembic import command
from esl_service.persistence.evidence_repository import (
    PromotionEvidenceRepository,
    RecordOutcomeRepository,
)
from esl_service.persistence.models import ConfigurationVersion
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository

#: Databases that must never be used as the dedicated integration target.
FORBIDDEN_DATABASE_NAMES = frozenset({"postgres", "template0", "template1"})

#: Revision the integration suite expects the dedicated database to carry.
REQUIRED_REVISION = "0005_record_outcomes"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _require_dedicated_database_url() -> str:
    """Return the configured test URL, or skip when no dedicated database exists."""

    database_url = os.environ.get("ESL_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ESL_TEST_DATABASE_URL is required for PostgreSQL integration tests")

    database_name = make_url(database_url).database
    if not database_name:
        raise RuntimeError("ESL_TEST_DATABASE_URL must name a dedicated database")
    if database_name in FORBIDDEN_DATABASE_NAMES:
        raise RuntimeError(
            "ESL_TEST_DATABASE_URL must not target a shared PostgreSQL system database"
        )

    production_database_name = os.environ.get("ESL_PRODUCTION_DATABASE_NAME")
    if production_database_name and database_name == production_database_name:
        raise RuntimeError(
            "ESL_TEST_DATABASE_URL must not target the configured production database"
        )
    return database_url


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    """Migrate the dedicated database to the revision under test exactly once."""

    database_url = _require_dedicated_database_url()

    previous_url = os.environ.get("ESL_DATABASE_URL")
    os.environ["ESL_DATABASE_URL"] = database_url
    try:
        config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_REPOSITORY_ROOT / "alembic"))
        command.upgrade(config, REQUIRED_REVISION)
    finally:
        if previous_url is None:
            os.environ.pop("ESL_DATABASE_URL", None)
        else:
            os.environ["ESL_DATABASE_URL"] = previous_url
    return database_url


@pytest.fixture(scope="session")
def engine(migrated_database_url: str) -> Iterator[Engine]:
    """Provide one engine for the migrated non-production database."""

    created = create_engine(migrated_database_url)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """Provide a connection whose outer transaction is always rolled back."""

    opened = engine.connect()
    transaction = opened.begin()
    try:
        yield opened
    finally:
        if transaction.is_active:
            transaction.rollback()
        opened.close()


@pytest.fixture
def session(connection: Connection) -> Iterator[Session]:
    """Provide a session bound to the rolled-back connection transaction."""

    opened = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def execution_repository(session: Session) -> ExecutionRepository:
    """Provide the existing execution repository against the same transaction."""

    return ExecutionRepository(session)


@pytest.fixture
def snapshot_repository(session: Session) -> SnapshotRepository:
    """Provide the canonical-snapshot repository against the same transaction."""

    return SnapshotRepository(session)


@pytest.fixture
def configuration_version_id(session: Session) -> UUID:
    """Every execution references exactly one configuration version (FR-025)."""

    version = ConfigurationVersion(
        environment="development",
        schema_version="config-v1",
        content_hash="c" * 64,
        sanitized_snapshot={"stores": ["075", "084"]},
        activated_by="test",
    )
    session.add(version)
    session.flush()
    return version.id


@pytest.fixture
def promotion_repository(session: Session) -> PromotionEvidenceRepository:
    """Provide the promotion-evidence repository on the same transaction."""

    return PromotionEvidenceRepository(session)


@pytest.fixture
def outcome_repository(session: Session) -> RecordOutcomeRepository:
    """Provide the record-outcome repository on the same transaction."""

    return RecordOutcomeRepository(session)
