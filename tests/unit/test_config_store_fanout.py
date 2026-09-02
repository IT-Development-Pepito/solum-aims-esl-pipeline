"""Per-store fan-out bounds are configuration (#92, FR-026, NFR-004).

The defaults are provisional operational values, not measured targets:
NFR-004 requires each to be reviewed against a workload baseline that has
not been captured yet. Both are in the sanitized configuration snapshot.
"""

import pytest
from pydantic import ValidationError

from esl_service.config import Settings, sanitized_configuration_snapshot

BASE = {
    "environment": "development",
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl_pipeline_dev",
    "internal_host": "127.0.0.1",
}


def test_defaults_are_bounded_and_provisional() -> None:
    settings = Settings.model_validate(BASE)

    assert settings.source_store_concurrency == 4
    assert settings.source_store_read_timeout_seconds == 120


@pytest.mark.parametrize(("field", "value"), [("source_store_concurrency", 0), ("source_store_concurrency", 33), ("source_store_read_timeout_seconds", 0)])
def test_out_of_range_values_are_refused(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, field: value})


def test_both_bounds_are_in_the_configuration_snapshot() -> None:
    snapshot = sanitized_configuration_snapshot(Settings.model_validate(BASE))

    assert snapshot["source_store_concurrency"] == 4
    assert snapshot["source_store_read_timeout_seconds"] == 120
