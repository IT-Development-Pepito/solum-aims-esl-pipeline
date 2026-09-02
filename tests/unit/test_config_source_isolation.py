"""The SQL Server isolation level is configuration, not a hard-coded contract (AD-020).

All three source databases run with snapshot isolation OFF, so the default
is READ COMMITTED. A DBA who enables snapshot isolation later switches the
tier by setting, and the value is part of the sanitized configuration
snapshot so every execution records which isolation it read under.
"""

import pytest
from pydantic import ValidationError

from esl_service.config import Settings, sanitized_configuration_snapshot

BASE = {
    "environment": "development",
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl_pipeline_dev",
    "internal_host": "127.0.0.1",
}


def test_the_default_is_read_committed() -> None:
    assert Settings.model_validate(BASE).source_sql_isolation_level == "READ COMMITTED"


def test_snapshot_can_be_selected() -> None:
    settings = Settings.model_validate({**BASE, "source_sql_isolation_level": "SNAPSHOT"})

    assert settings.source_sql_isolation_level == "SNAPSHOT"


@pytest.mark.parametrize("level", ["READ UNCOMMITTED", "NOLOCK", "snapshot", ""])
def test_anything_else_is_refused(level: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "source_sql_isolation_level": level})


def test_the_level_is_in_the_configuration_snapshot() -> None:
    snapshot = sanitized_configuration_snapshot(Settings.model_validate(BASE))

    assert snapshot["source_sql_isolation_level"] == "READ COMMITTED"
