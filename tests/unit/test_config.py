import pytest
from pydantic import ValidationError

from esl_service.config import Settings


def test_production_requires_internal_host() -> None:
    with pytest.raises(ValidationError, match="internal_host"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "",
            }
        )
