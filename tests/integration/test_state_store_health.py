"""State-store dependency health and configuration provenance (FR-024, FR-025).

The probe answers whether the service can reach its own state store, and an
execution records the exact configuration and rule version it ran under.
"""

from uuid import UUID

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from esl_service.config import (
    Settings,
    configuration_content_hash,
    sanitized_configuration_snapshot,
)
from esl_service.persistence.health import StateStoreProbe
from esl_service.persistence.models import ConfigurationVersion, WorkflowExecution
from esl_service.persistence.repository import ExecutionRepository
from esl_service.runtime.health import HealthService, HealthState
from tests.factories import new_execution


def test_state_store_probe_reports_a_reachable_database(engine: Engine) -> None:
    """A reachable state store is healthy and names no connection detail."""

    health = StateStoreProbe(engine).check()

    assert health.state is HealthState.HEALTHY
    assert health.required is True
    assert health.name == "state-store"


def test_state_store_probe_reports_an_unreachable_database() -> None:
    """An unreachable state store is unavailable, and its URL never leaks."""

    unreachable = create_engine(
        "postgresql+psycopg://esl_user:sup3rs3cret@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )
    try:
        report = HealthService(probes=(StateStoreProbe(unreachable),)).report()
    finally:
        unreachable.dispose()

    assert report.alive is True
    assert report.ready is False
    assert report.dependencies[0].state is HealthState.UNAVAILABLE
    assert "sup3rs3cret" not in str(report)
    assert "postgresql" not in str(report)


def test_ready_when_the_state_store_is_reachable(engine: Engine) -> None:
    """Readiness is true with a healthy required dependency and valid config."""

    assert HealthService(probes=(StateStoreProbe(engine),)).readiness() is True


def test_configuration_version_records_the_sanitized_snapshot(
    session: Session,
) -> None:
    """A configuration version stores the secret-free snapshot and its hash."""

    settings = Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql+psycopg://esl_user:sup3rs3cret@host/db",
            "internal_host": "127.0.0.1",
            "shadow_mode": True,
        }
    )
    snapshot = sanitized_configuration_snapshot(settings)
    version = ConfigurationVersion(
        environment=settings.environment,
        schema_version=str(snapshot["configuration_schema_version"]),
        content_hash=configuration_content_hash(snapshot),
        sanitized_snapshot=snapshot,
        activated_by="startup",
    )
    session.add(version)
    session.flush()
    session.expire_all()

    stored = session.get_one(ConfigurationVersion, version.id)
    assert "sup3rs3cret" not in str(stored.sanitized_snapshot)
    assert "database_url" not in stored.sanitized_snapshot
    assert len(stored.content_hash) == 64


def test_execution_records_its_configuration_and_rule_version(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """Every execution names the configuration and rules it ran under."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    session.flush()
    session.expire_all()

    stored = session.get_one(WorkflowExecution, execution.id)
    assert stored.configuration_version_id == configuration_version_id
    assert stored.rule_version == "rules-v1"
