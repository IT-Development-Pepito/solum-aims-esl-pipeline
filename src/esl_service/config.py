"""Runtime configuration and Windows secret-bundle trust boundary."""

import ctypes
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


class ProgramDataDirectoryProvider(Protocol):
    """Obtains the ProgramData known folder without trusting environment input."""

    def get_path(self) -> Path: ...


class WindowsProgramDataDirectoryProvider:
    """Resolves FOLDERID_ProgramData through the Windows Known Folder API."""

    _FOLDER_ID = _Guid(
        0x62AB5D82,
        0xFDC1,
        0x4DC3,
        (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97),
    )

    def get_path(self) -> Path:
        path = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(self._FOLDER_ID), 0, None, ctypes.byref(path)
        )
        if result != 0 or path.value is None:
            raise ValueError("secret_bundle_path trusted location is unavailable")
        try:
            return Path(path.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path)


@dataclass(frozen=True)
class AclEntry:
    """One allow ACE reduced to its principal SID and access mask."""

    sid: str
    access_mask: int


class SecretBundleAclReader(Protocol):
    """Reads the existence and allow ACEs used by the bundle policy."""

    def is_file(self, path: Path) -> bool: ...

    def is_directory(self, path: Path) -> bool: ...

    def allow_entries(self, path: Path) -> tuple[AclEntry, ...]: ...


class WindowsSecretBundleAclReader:
    """Windows-only DACL reader used only after production path validation."""

    _ALLOW_ACE_TYPES = frozenset({0, 5})

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def allow_entries(self, path: Path) -> tuple[AclEntry, ...]:
        try:
            import win32security  # type: ignore[import-untyped]

            descriptor = win32security.GetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                raise ValueError("secret_bundle_path ACL is missing")
            entries: list[AclEntry] = []
            for index in range(dacl.GetAceCount()):
                header, access_mask, sid = dacl.GetAce(index)
                if header[0] in self._ALLOW_ACE_TYPES:
                    entries.append(
                        AclEntry(
                            sid=win32security.ConvertSidToStringSid(sid),
                            access_mask=access_mask,
                        )
                    )
            return tuple(entries)
        except ValueError:
            raise
        except Exception:  # noqa: BLE001
            raise ValueError("secret_bundle_path ACL could not be verified") from None


class SecretBundlePathValidator(Protocol):
    """Checks the Windows-specific security boundary for a secret bundle."""

    def validate(self, path: Path, service_identity_sid: str) -> None: ...


class WindowsSecretBundlePathValidator:
    """Positively allows only the service SID, local administrators, and SYSTEM."""

    _ADMINISTRATORS_SID = "S-1-5-32-544"
    # SYSTEM is allowed because the Windows Service may run as LocalSystem.
    _LOCAL_SYSTEM_SID = "S-1-5-18"
    _DIRECTORY_REPLACEMENT_MASK = (
        0x00000002  # FILE_ADD_FILE / FILE_WRITE_DATA
        | 0x00000004  # FILE_ADD_SUBDIRECTORY / FILE_APPEND_DATA
        | 0x00000010  # FILE_WRITE_EA
        | 0x00000040  # FILE_DELETE_CHILD
        | 0x00000100  # FILE_WRITE_ATTRIBUTES
        | 0x00010000  # DELETE
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
        | 0x10000000  # GENERIC_ALL
        | 0x40000000  # GENERIC_WRITE
    )

    def __init__(self, reader: SecretBundleAclReader | None = None) -> None:
        self._reader = reader or WindowsSecretBundleAclReader()

    def validate(self, path: Path, service_identity_sid: str) -> None:
        if not self._reader.is_file(path):
            raise ValueError("secret_bundle_path must reference an existing bundle")
        if not self._reader.is_directory(path.parent):
            raise ValueError("secret_bundle_path parent directory is missing")

        approved_sids = frozenset(
            {
                service_identity_sid,
                self._ADMINISTRATORS_SID,
                self._LOCAL_SYSTEM_SID,
            }
        )
        for entry in self._reader.allow_entries(path):
            if entry.sid not in approved_sids:
                raise ValueError("secret_bundle_path ACL permits non-approved principal")
        for entry in self._reader.allow_entries(path.parent):
            if (
                entry.sid not in approved_sids
                and entry.access_mask & self._DIRECTORY_REPLACEMENT_MASK
            ):
                raise ValueError(
                    "secret_bundle_path directory ACL permits non-approved principal"
                )


class Settings(BaseSettings):
    """Configuration loaded from explicit values or ``ESL_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="ESL_", extra="forbid")

    environment: Literal["development", "staging", "production"]
    database_url: str
    internal_host: str
    shadow_mode: bool = True
    secret_bundle_path: Path = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")
    service_identity_sid: str = Field(default="", repr=False)
    program_data_directory_provider_factory: ClassVar[
        Callable[[], ProgramDataDirectoryProvider]
    ] = WindowsProgramDataDirectoryProvider
    secret_bundle_path_validator_factory: ClassVar[
        Callable[[], SecretBundlePathValidator]
    ] = WindowsSecretBundlePathValidator

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment == "production" and not self.internal_host.strip():
            raise ValueError("internal_host must be configured for production")
        if self.environment != "production":
            return self
        if not self.service_identity_sid.strip():
            raise ValueError("service_identity_sid must be configured for production")

        canonical_bundle_path = self.secret_bundle_path.resolve(strict=False)
        program_data_directory = type(self).program_data_directory_provider_factory().get_path()
        approved_bundle_path = (
            program_data_directory / "SOLUM" / "ESL" / "secrets.dpapi"
        ).resolve(strict=False)
        if (
            not self.secret_bundle_path.is_absolute()
            or canonical_bundle_path != approved_bundle_path
        ):
            raise ValueError(
                "secret_bundle_path must use the approved ProgramData location"
            )

        validator = type(self).secret_bundle_path_validator_factory()
        validator.validate(canonical_bundle_path, self.service_identity_sid)
        return self
