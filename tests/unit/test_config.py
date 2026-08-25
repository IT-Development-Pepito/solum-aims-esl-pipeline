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


def test_production_rejects_relative_secret_bundle_path() -> None:
    with pytest.raises(ValidationError, match="secret_bundle_path"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "esl.internal",
                "secret_bundle_path": "secrets.dpapi",
            }
        )


def test_production_rejects_secret_bundle_path_outside_programdata() -> None:
    with pytest.raises(ValidationError, match="secret_bundle_path"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "esl.internal",
                "secret_bundle_path": r"D:\workspace\secrets.dpapi",
            }
        )


class _InsecureAclValidator:
    def validate(self, _path) -> None:
        raise ValueError("secret_bundle_path ACL is insecure")


def test_production_rejects_insecure_secret_bundle_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Settings,
        "secret_bundle_path_validator_factory",
        _InsecureAclValidator,
    )
    with pytest.raises(ValidationError, match="secret_bundle_path ACL is insecure"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "esl.internal",
                "secret_bundle_path": r"C:\ProgramData\SOLUM\ESL\secrets.dpapi",
            },
        )
