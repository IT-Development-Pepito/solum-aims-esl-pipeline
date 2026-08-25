from pathlib import Path

import pytest
from pydantic import ValidationError

from esl_service.config import AclEntry, Settings, WindowsSecretBundlePathValidator


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
                "service_identity_sid": "S-1-5-21-test-service",
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
                "service_identity_sid": "S-1-5-21-test-service",
            }
        )


def test_production_requires_service_identity_sid() -> None:
    with pytest.raises(ValidationError, match="service_identity_sid"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "esl.internal",
                "secret_bundle_path": r"C:\ProgramData\SOLUM\ESL\secrets.dpapi",
            }
        )


class _InsecureAclValidator:
    def validate(self, _path: Path, _service_identity_sid: str) -> None:
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
                "service_identity_sid": "S-1-5-21-test-service",
            },
        )


class _TrustedProgramDataProvider:
    def get_path(self) -> Path:
        return Path(r"C:\TrustedProgramData")


class _AcceptingAclValidator:
    def validate(self, _path: Path, _service_identity_sid: str) -> None:
        return None


def test_production_uses_trusted_known_folder_not_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ProgramData", r"D:\untrusted-programdata")
    monkeypatch.setattr(
        Settings,
        "program_data_directory_provider_factory",
        _TrustedProgramDataProvider,
    )
    monkeypatch.setattr(
        Settings,
        "secret_bundle_path_validator_factory",
        _AcceptingAclValidator,
    )

    settings = Settings.model_validate(
        {
            "environment": "production",
            "database_url": "postgresql://state",
            "internal_host": "esl.internal",
            "secret_bundle_path": r"C:\TrustedProgramData\SOLUM\ESL\secrets.dpapi",
            "service_identity_sid": "S-1-5-21-test-service",
        }
    )

    assert settings.secret_bundle_path == Path(
        r"C:\TrustedProgramData\SOLUM\ESL\secrets.dpapi"
    )


class _StaticAclReader:
    def __init__(
        self,
        file_entries: tuple[AclEntry, ...],
        directory_entries: tuple[AclEntry, ...],
    ) -> None:
        self._file_entries = file_entries
        self._directory_entries = directory_entries

    def is_file(self, _path: Path) -> bool:
        return True

    def is_directory(self, _path: Path) -> bool:
        return True

    def allow_entries(self, path: Path) -> tuple[AclEntry, ...]:
        if path.name == "secrets.dpapi":
            return self._file_entries
        return self._directory_entries


def test_production_acl_rejects_arbitrary_file_account() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_entries=(AclEntry("S-1-5-21-arbitrary", 1),),
            directory_entries=(),
        )
    )

    with pytest.raises(ValueError, match="non-approved principal"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"),
            "S-1-5-21-test-service",
        )


def test_production_acl_rejects_arbitrary_directory_write() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_entries=(),
            directory_entries=(AclEntry("S-1-5-21-arbitrary", 0x40000000),),
        )
    )

    with pytest.raises(ValueError, match="directory ACL permits non-approved principal"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"),
            "S-1-5-21-test-service",
        )


def test_production_acl_accepts_service_admin_and_system_only() -> None:
    service_sid = "S-1-5-21-test-service"
    allowed_entries = (
        AclEntry(service_sid, 1),
        AclEntry("S-1-5-32-544", 1),
        AclEntry("S-1-5-18", 1),
    )
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(allowed_entries, allowed_entries)
    )

    validator.validate(
        Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"), service_sid
    )
