"""Hosting settings for the internal API listener (FR-029, #28).

The listener binds only to ``ESL_INTERNAL_HOST`` (already validated for
production) on ``ESL_INTERNAL_PORT``. The port is part of the sanitized
configuration snapshot; it is not a secret.
"""

import pytest
from pydantic import ValidationError

from esl_service.config import Settings, sanitized_configuration_snapshot

BASE = {
    "environment": "development",
    "database_url": "postgresql+psycopg://esl@localhost:5432/esl_pipeline_dev",
    "internal_host": "127.0.0.1",
}


def test_the_internal_port_defaults_to_8000() -> None:
    assert Settings.model_validate(BASE).internal_port == 8000


def test_the_internal_port_is_configurable() -> None:
    assert Settings.model_validate({**BASE, "internal_port": 8443}).internal_port == 8443


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_an_impossible_port_is_refused(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "internal_port": port})


def test_the_port_is_in_the_configuration_snapshot() -> None:
    snapshot = sanitized_configuration_snapshot(Settings.model_validate(BASE))

    assert snapshot["internal_port"] == 8000


def test_metrics_use_a_bounded_configurable_recent_run_window() -> None:
    settings = Settings.model_validate(BASE)
    configured = Settings.model_validate({**BASE, "metrics_run_limit": 7})

    assert settings.metrics_run_limit == 20
    assert configured.metrics_run_limit == 7
    assert sanitized_configuration_snapshot(configured)["metrics_run_limit"] == 7


def test_metrics_run_limit_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "metrics_run_limit": 0})
