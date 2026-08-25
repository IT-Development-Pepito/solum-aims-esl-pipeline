import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from esl_service.config import (
    AclEntry,
    AclInspection,
    ResolvedWindowsIdentity,
    Settings,
    WindowsSecretBundleAclReader,
    WindowsSecretBundlePathValidator,
    WindowsServiceIdentityResolver,
    WindowsServiceIdentityValidator,
)

_SERVICE_SID = "S-1-5-21-111-222-333-4444"
_WINDOWS_SID_TYPE_USER = 1
_WINDOWS_SID_TYPE_GROUP = 2
_WINDOWS_SID_TYPE_WELL_KNOWN_GROUP = 5


class _StaticServiceIdentityResolver:
    def __init__(self, identities: dict[str, ResolvedWindowsIdentity]) -> None:
        self._identities = identities

    def resolve(self, sid: str) -> ResolvedWindowsIdentity:
        try:
            return self._identities[sid.upper()]
        except KeyError:
            raise ValueError("service_identity_sid could not be verified") from None


@pytest.fixture(autouse=True)
def _inject_test_service_identity_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ResolvedWindowsIdentity(
        sid=_SERVICE_SID,
        account_name="test-esl-service",
        domain="TEST",
        account_type=_WINDOWS_SID_TYPE_USER,
    )
    monkeypatch.setattr(
        Settings,
        "service_identity_validator_factory",
        lambda: WindowsServiceIdentityValidator(
            _StaticServiceIdentityResolver({_SERVICE_SID: identity})
        ),
    )


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
                "service_identity_sid": _SERVICE_SID,
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
                "service_identity_sid": _SERVICE_SID,
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
                "service_identity_sid": _SERVICE_SID,
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
            "service_identity_sid": _SERVICE_SID,
        }
    )

    assert settings.secret_bundle_path == Path(
        r"C:\TrustedProgramData\SOLUM\ESL\secrets.dpapi"
    )


class _StaticAclReader:
    def __init__(
        self,
        file_owner_sid: str,
        file_entries: tuple[AclEntry, ...],
        directory_owner_sid: str,
        directory_entries: tuple[AclEntry, ...],
    ) -> None:
        self._file_owner_sid = file_owner_sid
        self._file_entries = file_entries
        self._directory_owner_sid = directory_owner_sid
        self._directory_entries = directory_entries

    def read_file(self, _path: Path) -> AclInspection:
        return AclInspection(self._file_owner_sid, self._file_entries)

    def read_directory(self, _path: Path) -> AclInspection:
        return AclInspection(self._directory_owner_sid, self._directory_entries)


def test_production_acl_rejects_arbitrary_file_account() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_owner_sid=_SERVICE_SID,
            file_entries=(AclEntry("S-1-5-21-arbitrary", 1),),
            directory_owner_sid=_SERVICE_SID,
            directory_entries=(),
        )
    )

    with pytest.raises(ValueError, match="non-approved principal"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"),
            _SERVICE_SID,
        )


def test_production_acl_rejects_arbitrary_directory_write() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_owner_sid=_SERVICE_SID,
            file_entries=(),
            directory_owner_sid=_SERVICE_SID,
            directory_entries=(AclEntry("S-1-5-21-arbitrary", 0x40000000),),
        )
    )

    with pytest.raises(ValueError, match="directory ACL permits non-approved principal"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"),
            _SERVICE_SID,
        )


def test_production_acl_accepts_service_admin_and_system_only() -> None:
    service_sid = _SERVICE_SID
    allowed_entries = (
        AclEntry(service_sid, 1),
        AclEntry("S-1-5-32-544", 1),
        AclEntry("S-1-5-18", 1),
    )
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(service_sid, allowed_entries, service_sid, allowed_entries)
    )

    validator.validate(
        Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"), service_sid
    )


def test_production_acl_rejects_unapproved_file_owner() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_owner_sid="S-1-5-21-arbitrary",
            file_entries=(),
            directory_owner_sid=_SERVICE_SID,
            directory_entries=(),
        )
    )

    with pytest.raises(ValueError, match="file owner is not approved"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"), _SERVICE_SID
        )


def test_production_acl_rejects_unsupported_allow_ace_type() -> None:
    validator = WindowsSecretBundlePathValidator(
        _StaticAclReader(
            file_owner_sid=_SERVICE_SID,
            file_entries=(AclEntry(None, 1, ace_type=9),),
            directory_owner_sid=_SERVICE_SID,
            directory_entries=(),
        )
    )

    with pytest.raises(ValueError, match="unsupported allow ACE type"):
        validator.validate(
            Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"), _SERVICE_SID
        )


@pytest.mark.parametrize("ace_type", [4, 255])
def test_windows_acl_reader_fails_closed_for_uninterpreted_ace_types(
    ace_type: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _StaticDacl:
        def GetAceCount(self) -> int:
            return 1

        def GetAce(self, _index: int) -> tuple[tuple[int, int], int]:
            return ((ace_type, 0), 1)

    class _StaticDescriptor:
        def GetSecurityDescriptorDacl(self) -> _StaticDacl:
            return _StaticDacl()

        def GetSecurityDescriptorOwner(self) -> str:
            return _SERVICE_SID

    monkeypatch.setitem(
        sys.modules,
        "win32security",
        SimpleNamespace(
            DACL_SECURITY_INFORMATION=4,
            OWNER_SECURITY_INFORMATION=1,
            GetFileSecurity=lambda *_: _StaticDescriptor(),
            ConvertSidToStringSid=lambda sid: sid,
        ),
    )
    bundle_path = tmp_path / "secrets.dpapi"
    bundle_path.write_bytes(b"test-bundle")
    validator = WindowsSecretBundlePathValidator(WindowsSecretBundleAclReader())

    with pytest.raises(ValueError, match="unsupported allow ACE type"):
        validator.validate(bundle_path, _SERVICE_SID)


def test_windows_identity_resolver_parses_and_looks_up_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ParsedSid:
        def IsValid(self) -> bool:
            return True

    parsed_sid = _ParsedSid()
    monkeypatch.setitem(
        sys.modules,
        "win32security",
        SimpleNamespace(
            ConvertStringSidToSid=lambda _sid: parsed_sid,
            ConvertSidToStringSid=lambda _sid: _SERVICE_SID,
            LookupAccountSid=lambda _system, _sid: (
                "test-esl-service",
                "TEST",
                _WINDOWS_SID_TYPE_USER,
            ),
        ),
    )

    identity = WindowsServiceIdentityResolver().resolve(_SERVICE_SID.lower())

    assert identity == ResolvedWindowsIdentity(
        sid=_SERVICE_SID,
        account_name="test-esl-service",
        domain="TEST",
        account_type=_WINDOWS_SID_TYPE_USER,
    )


@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        (
            ResolvedWindowsIdentity(
                "S-1-5-4",
                "INTERACTIVE",
                "NT AUTHORITY",
                _WINDOWS_SID_TYPE_WELL_KNOWN_GROUP,
            ),
            "interactive well-known principal",
        ),
        (
            ResolvedWindowsIdentity(
                "S-1-5-6",
                "SERVICE",
                "NT AUTHORITY",
                _WINDOWS_SID_TYPE_WELL_KNOWN_GROUP,
            ),
            "service well-known principal",
        ),
        (
            ResolvedWindowsIdentity(
                "S-1-5-21-111-222-333-513",
                "Domain Users",
                "TEST",
                _WINDOWS_SID_TYPE_GROUP,
            ),
            "ordinary account group",
        ),
        (
            ResolvedWindowsIdentity(
                "S-1-5-80-111-222-333-444-555",
                "spoofed-service",
                "NT AUTHORITY",
                _WINDOWS_SID_TYPE_WELL_KNOWN_GROUP,
            ),
            "service SID outside NT SERVICE namespace",
        ),
    ],
)
def test_service_identity_validator_rejects_broad_or_group_principals(
    identity: ResolvedWindowsIdentity,
    reason: str,
) -> None:
    validator = WindowsServiceIdentityValidator(
        _StaticServiceIdentityResolver({identity.sid: identity})
    )

    with pytest.raises(ValueError, match="real service account or service SID"):
        validator.validate(identity.sid)


@pytest.mark.parametrize(
    "identity",
    [
        ResolvedWindowsIdentity(
            _SERVICE_SID,
            "test-esl-service",
            "TEST",
            _WINDOWS_SID_TYPE_USER,
        ),
        ResolvedWindowsIdentity(
            "S-1-5-80-111-222-333-444-555",
            "test-esl-service",
            "NT SERVICE",
            _WINDOWS_SID_TYPE_WELL_KNOWN_GROUP,
        ),
    ],
)
def test_service_identity_validator_accepts_real_service_identity(
    identity: ResolvedWindowsIdentity,
) -> None:
    validator = WindowsServiceIdentityValidator(
        _StaticServiceIdentityResolver({identity.sid: identity})
    )

    assert validator.validate(identity.sid.lower()) == identity.sid


@pytest.mark.parametrize("service_sid", ["not-a-sid", "S-1-1-0", "S-1-5-32-544"])
def test_production_rejects_invalid_or_broad_service_identity_sid(
    service_sid: str,
) -> None:
    with pytest.raises(ValidationError, match="service_identity_sid"):
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://state",
                "internal_host": "esl.internal",
                "secret_bundle_path": r"C:\ProgramData\SOLUM\ESL\secrets.dpapi",
                "service_identity_sid": service_sid,
            }
        )
