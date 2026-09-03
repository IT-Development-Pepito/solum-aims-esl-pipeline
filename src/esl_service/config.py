"""Runtime configuration and Windows secret-bundle trust boundary."""

import ctypes
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal, NoReturn, Protocol
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from esl_service.domain.authorization import (
    InvalidRoleAssignment,
    Role,
    parse_role_assignments,
)
from esl_service.domain.failures import RetryPolicy
from esl_service.domain.serialization import JSONValue, canonical_hash


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
class ResolvedWindowsIdentity:
    """Canonical Windows SID and its resolved account classification."""

    sid: str
    account_name: str
    domain: str
    account_type: int


class ServiceIdentityResolver(Protocol):
    """Parses a SID and resolves its Windows account classification."""

    def resolve(self, sid: str) -> ResolvedWindowsIdentity: ...


class WindowsServiceIdentityResolver:
    """Uses Windows SID parsing and account lookup without trusting SID text."""

    def resolve(self, sid: str) -> ResolvedWindowsIdentity:
        try:
            import win32security  # type: ignore[import-untyped]

            parsed_sid = win32security.ConvertStringSidToSid(sid)
            if not parsed_sid.IsValid():
                raise ValueError
            canonical_sid = win32security.ConvertSidToStringSid(parsed_sid).upper()
            account_name, domain, account_type = win32security.LookupAccountSid(
                None, parsed_sid
            )
            if not account_name.strip() or not domain.strip():
                raise ValueError
            return ResolvedWindowsIdentity(
                sid=canonical_sid,
                account_name=account_name,
                domain=domain,
                account_type=account_type,
            )
        except Exception:  # noqa: BLE001
            raise ValueError("service_identity_sid could not be verified") from None


@dataclass(frozen=True)
class ResolvedWindowsServiceAccount:
    """Account configured to run one named Windows service."""

    service_name: str
    account_name: str
    domain: str
    sid: str
    account_type: int


class ServiceAccountResolver(Protocol):
    """Resolves the logon identity configured for a named Windows service."""

    def resolve(self, service_name: str) -> ResolvedWindowsServiceAccount: ...


class WindowsServiceAccountResolver:
    """Reads a service's configured logon account through Windows APIs."""

    def resolve(self, service_name: str) -> ResolvedWindowsServiceAccount:
        normalized_service_name = service_name.strip()
        if not normalized_service_name:
            raise ValueError("windows_service_name could not be verified")

        service_manager = None
        service = None
        try:
            import win32security
            import win32service  # type: ignore[import-untyped]

            service_manager = win32service.OpenSCManager(
                None, None, win32service.SC_MANAGER_CONNECT
            )
            service = win32service.OpenService(
                service_manager,
                normalized_service_name,
                win32service.SERVICE_QUERY_CONFIG,
            )
            configuration = win32service.QueryServiceConfig(service)
            service_start_name = configuration[7]
            if not isinstance(service_start_name, str) or not service_start_name.strip():
                raise ValueError
            account_sid, _, account_type = win32security.LookupAccountName(
                None, service_start_name
            )
            account_name, domain, resolved_account_type = (
                win32security.LookupAccountSid(None, account_sid)
            )
            if account_type != resolved_account_type:
                raise ValueError
            canonical_sid = win32security.ConvertSidToStringSid(account_sid).upper()
            if not account_name.strip() or not domain.strip():
                raise ValueError
            return ResolvedWindowsServiceAccount(
                service_name=normalized_service_name,
                account_name=account_name,
                domain=domain,
                sid=canonical_sid,
                account_type=resolved_account_type,
            )
        except Exception:  # noqa: BLE001
            raise ValueError("windows_service_name could not be verified") from None
        finally:
            if service is not None:
                win32service.CloseServiceHandle(service)
            if service_manager is not None:
                win32service.CloseServiceHandle(service_manager)


class ServiceIdentityValidator(Protocol):
    """Accepts only a resolved service account or per-service SID."""

    def validate(self, sid: str, service_name: str) -> str: ...


class WindowsServiceIdentityValidator:
    """Rejects broad and group principals even when their SID is resolvable."""

    _SID_TYPE_USER = 1
    _SID_TYPE_WELL_KNOWN_GROUP = 5
    _SERVICE_SID_DOMAIN = "NT SERVICE"
    _ALL_SERVICES_SID = "S-1-5-80-0"
    _SERVICE_SID_PATTERN = re.compile(
        r"S-1-5-80-(\d+)-(\d+)-(\d+)-(\d+)-(\d+)"
    )
    _MAX_SUBAUTHORITY = 0xFFFFFFFF
    _BUILTIN_ADMINISTRATOR_RID = "500"

    def __init__(
        self,
        resolver: ServiceIdentityResolver | None = None,
        service_account_resolver: ServiceAccountResolver | None = None,
    ) -> None:
        self._resolver = resolver or WindowsServiceIdentityResolver()
        self._service_account_resolver = (
            service_account_resolver or WindowsServiceAccountResolver()
        )

    def validate(self, sid: str, service_name: str) -> str:
        identity = self._resolver.resolve(sid)
        canonical_sid = identity.sid.upper()
        normalized_service_name = service_name.strip()
        if (
            sid.strip().upper() != canonical_sid
            or canonical_sid == self._ALL_SERVICES_SID
            or not normalized_service_name
        ):
            self._raise_invalid_identity()

        service_account = self._service_account_resolver.resolve(
            normalized_service_name
        )
        if service_account.service_name.casefold() != normalized_service_name.casefold():
            self._raise_invalid_identity()

        if identity.account_type == self._SID_TYPE_USER:
            if canonical_sid.rsplit("-", maxsplit=1)[-1] == (
                self._BUILTIN_ADMINISTRATOR_RID
            ):
                self._raise_invalid_identity()
            if (
                service_account.account_type != self._SID_TYPE_USER
                or service_account.sid.upper() != canonical_sid
                or service_account.account_name.casefold()
                != identity.account_name.casefold()
                or service_account.domain.casefold() != identity.domain.casefold()
            ):
                self._raise_invalid_identity()
            return canonical_sid

        service_sid_match = self._SERVICE_SID_PATTERN.fullmatch(canonical_sid)
        if (
            identity.account_type == self._SID_TYPE_WELL_KNOWN_GROUP
            and service_sid_match is not None
            and all(
                int(subauthority) <= self._MAX_SUBAUTHORITY
                and str(int(subauthority)) == subauthority
                for subauthority in service_sid_match.groups()
            )
            and identity.domain.upper() == self._SERVICE_SID_DOMAIN
            and identity.account_name.casefold() == normalized_service_name.casefold()
        ):
            return canonical_sid
        self._raise_invalid_identity()

    @staticmethod
    def _raise_invalid_identity() -> NoReturn:
        raise ValueError(
            "service_identity_sid must identify a real service account or service SID"
        )


@dataclass(frozen=True)
class AclEntry:
    """One allow ACE reduced to its principal SID and access mask."""

    sid: str | None
    access_mask: int
    ace_type: int = 0


@dataclass(frozen=True)
class AclInspection:
    """Owner SID and all DACL ACEs that require policy evaluation."""

    owner_sid: str
    entries: tuple[AclEntry, ...]


class SecretBundleAclReader(Protocol):
    """Reads the existence and allow ACEs used by the bundle policy."""

    def read_file(self, path: Path) -> AclInspection: ...

    def read_directory(self, path: Path) -> AclInspection: ...


class WindowsSecretBundleAclReader:
    """Windows-only DACL reader used only after production path validation."""

    _ALLOW_CAPABLE_ACE_TYPES = frozenset({0, 4, 5, 9, 11})
    _KNOWN_NON_ALLOW_ACE_TYPES = frozenset(
        {1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21}
    )

    def read_file(self, path: Path) -> AclInspection:
        if not path.is_file():
            raise ValueError("secret_bundle_path must reference an existing bundle")
        return self._read_acl(path)

    def read_directory(self, path: Path) -> AclInspection:
        if not path.is_dir():
            raise ValueError("secret_bundle_path parent directory is missing")
        return self._read_acl(path)

    def _read_acl(self, path: Path) -> AclInspection:
        try:
            import win32security

            descriptor = win32security.GetFileSecurity(
                str(path),
                win32security.DACL_SECURITY_INFORMATION
                | win32security.OWNER_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                raise ValueError("secret_bundle_path ACL is missing")
            owner_sid = win32security.ConvertSidToStringSid(
                descriptor.GetSecurityDescriptorOwner()
            )
            entries: list[AclEntry] = []
            for index in range(dacl.GetAceCount()):
                ace = dacl.GetAce(index)
                header = ace[0]
                if header[0] == 0:
                    _, access_mask, sid = ace
                    entries.append(
                        AclEntry(
                            sid=win32security.ConvertSidToStringSid(sid),
                            access_mask=access_mask,
                        )
                    )
                elif (
                    header[0] in self._ALLOW_CAPABLE_ACE_TYPES
                    or header[0] not in self._KNOWN_NON_ALLOW_ACE_TYPES
                ):
                    entries.append(AclEntry(None, 0, ace_type=header[0]))
            return AclInspection(owner_sid, tuple(entries))
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
        file_acl = self._reader.read_file(path)
        directory_acl = self._reader.read_directory(path.parent)

        approved_sids = frozenset(
            {
                service_identity_sid,
                self._ADMINISTRATORS_SID,
                self._LOCAL_SYSTEM_SID,
            }
        )
        if file_acl.owner_sid not in approved_sids:
            raise ValueError("secret_bundle_path file owner is not approved")
        if directory_acl.owner_sid not in approved_sids:
            raise ValueError("secret_bundle_path directory owner is not approved")
        for entry in file_acl.entries:
            self._validate_supported_ace(entry)
            if entry.sid not in approved_sids:
                raise ValueError("secret_bundle_path ACL permits non-approved principal")
        for entry in directory_acl.entries:
            self._validate_supported_ace(entry)
            if (
                entry.sid not in approved_sids
                and entry.access_mask & self._DIRECTORY_REPLACEMENT_MASK
            ):
                raise ValueError(
                    "secret_bundle_path directory ACL permits non-approved principal"
                )

    @staticmethod
    def _validate_supported_ace(entry: AclEntry) -> None:
        if entry.ace_type != 0:
            raise ValueError("secret_bundle_path ACL contains unsupported allow ACE type")


class Settings(BaseSettings):
    """Configuration loaded from explicit values or ``ESL_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="ESL_", extra="forbid")

    environment: Literal["development", "staging", "production"]
    database_url: str
    internal_host: str
    # The internal API listener binds to internal_host on this port only (#28).
    internal_port: int = Field(default=8000, ge=1, le=65535)
    shadow_mode: bool = True
    secret_bundle_path: Path = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")
    # Retention durations are UNKNOWN / NEEDS-DISCOVERY, so none is defaulted.
    # Purge stays disabled until the business supplies every applicable value.
    # Retry behaviour is operational configuration (FR-015). These defaults are
    # provisional: NFR-004 requires targets to come from a measured baseline,
    # and no workload baseline has been captured yet.
    retry_max_attempts: int = 3
    retry_timeout_seconds: Decimal = Decimal(30)
    retry_initial_backoff_seconds: Decimal = Decimal(1)
    retry_max_backoff_seconds: Decimal = Decimal(60)
    retry_jitter_ratio: Decimal = Decimal("0.5")
    retention_purge_enabled: bool = False
    audit_core_days: int | None = None
    detailed_evidence_days: int | None = None
    compatibility_days: int | None = None
    service_identity_sid: str = Field(default="", repr=False)
    windows_service_name: str = ""
    # identity=role[,role];identity=role -- who may perform manual operations
    # (FR-023, AD-018). Not a secret: it is part of the configuration snapshot.
    operator_roles: str = ""
    # Source and AIMS connections carry only non-secret parts (#78, AD-017).
    # Every password is read from the DPAPI bundle by a fixed key; there is
    # deliberately no field that could hold one. Empty means unconfigured.
    # One read-only account and one ODBC driver cover every SQL Server tier,
    # both confirmed by the source owner. DBWH_8555 and ESL share the instance.
    source_sql_host: str = ""
    source_sql_username: str = ""
    source_sql_driver: str = "ODBC Driver 18 for SQL Server"
    source_sql_trust_server_certificate: bool = True
    # AD-020: every source database runs with snapshot isolation OFF, so reads
    # default to READ COMMITTED; a DBA who enables snapshot isolation switches
    # the tiers here. Recorded in every read's provenance.
    source_sql_isolation_level: Literal["READ COMMITTED", "SNAPSHOT"] = "READ COMMITTED"
    source_warehouse_database: str = "DBWH_8555"
    legacy_baseline_database: str = "ESL"
    source_pepito_ho_host: str = ""
    source_pepito_ho_database: str = "PEPITO_HO"
    # Per-store fan-out bounds (#92). Provisional operational defaults, not
    # measured targets (NFR-004): review against a workload baseline.
    source_store_concurrency: int = Field(default=4, ge=1, le=32)
    source_store_read_timeout_seconds: int = Field(default=120, ge=1)
    # How many executions the worker (#102) runs at once. One is the safe
    # default until a workload baseline exists (NFR-004); the scope lease
    # (#17) keeps one run per workflow and store regardless.
    worker_concurrency: int = Field(default=1, ge=1, le=8)
    aims_host: str = ""
    aims_port: int = 5432
    aims_portal_database: str = ""
    aims_portal_username: str = ""
    aims_core_database: str = ""
    aims_core_username: str = ""
    program_data_directory_provider_factory: ClassVar[
        Callable[[], ProgramDataDirectoryProvider]
    ] = WindowsProgramDataDirectoryProvider
    secret_bundle_path_validator_factory: ClassVar[
        Callable[[], SecretBundlePathValidator]
    ] = WindowsSecretBundlePathValidator
    service_identity_validator_factory: ClassVar[
        Callable[[], ServiceIdentityValidator]
    ] = WindowsServiceIdentityValidator

    @model_validator(mode="after")
    def validate_retry_configuration(self) -> "Settings":
        """Refuse a retry configuration that could never make progress."""

        for name in (
            "retry_max_attempts",
            "retry_timeout_seconds",
            "retry_initial_backoff_seconds",
            "retry_max_backoff_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not Decimal(0) <= self.retry_jitter_ratio <= Decimal(1):
            raise ValueError("retry_jitter_ratio must be between zero and one")
        if self.retry_max_backoff_seconds < self.retry_initial_backoff_seconds:
            raise ValueError(
                "retry_max_backoff_seconds must not be below "
                "retry_initial_backoff_seconds"
            )
        return self

    @model_validator(mode="after")
    def validate_retention_configuration(self) -> "Settings":
        """Refuse an enabled purge that has no explicit retention duration.

        Deleting durable evidence on an assumed period would be a silent data
        loss, so every applicable duration is mandatory once purge is enabled
        and none is ever defaulted (architecture 5.8).
        """

        if not self.retention_purge_enabled:
            return self
        for name in (
            "audit_core_days",
            "detailed_evidence_days",
            "compatibility_days",
        ):
            value = getattr(self, name)
            if value is None or value <= 0:
                raise ValueError(
                    f"{name} must be a positive number of days when "
                    "retention_purge_enabled is true"
                )
        return self

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment == "production" and not self.internal_host.strip():
            raise ValueError("internal_host must be configured for production")
        if self.environment != "production":
            return self
        if not self.service_identity_sid.strip():
            raise ValueError("service_identity_sid must be configured for production")
        if not self.windows_service_name.strip():
            raise ValueError("windows_service_name must be configured for production")
        normalized_service_sid = (
            type(self)
            .service_identity_validator_factory()
            .validate(self.service_identity_sid, self.windows_service_name)
        )

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
        validator.validate(canonical_bundle_path, normalized_service_sid)
        return self


#: Version of the sanitized configuration snapshot shape.
CONFIGURATION_SCHEMA_VERSION = "configuration-v1"

#: Settings excluded from a configuration version because they carry, or point
#: at, a secret or a host-specific location rather than business configuration.
SECRET_BEARING_SETTINGS = frozenset({"database_url", "secret_bundle_path"})


@dataclass(frozen=True)
class ConfigurationProblem:
    """One startup configuration fault, named without its value."""

    key: str
    message: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("key must not be blank")


def describe_configuration_problems(
    error: "ValidationError",
) -> tuple[ConfigurationProblem, ...]:
    """Convert a validation error into key-only problems (FR-025, NFR-009).

    Pydantic's error payload includes the rejected input, which for a database
    URL is a credential. Only the key path and the validator's own message are
    kept, and the message is used verbatim only because it never contains the
    input value.
    """

    problems = {
        ConfigurationProblem(
            key=".".join(str(part) for part in item["loc"]) or "configuration",
            message=str(item["msg"]),
        )
        for item in error.errors()
    }
    return tuple(sorted(problems, key=lambda problem: problem.key))


def validate_startup_configuration(
    values: Mapping[str, object],
) -> tuple["Settings | None", tuple[ConfigurationProblem, ...]]:
    """Validate configuration at startup, reporting faults by key only.

    Returns the settings when valid, or ``None`` with the problems that must be
    corrected. Invalid configuration prevents readiness rather than allowing a
    partially configured service to accept work.
    """

    try:
        settings = Settings.model_validate(dict(values))
    except ValidationError as error:
        return None, describe_configuration_problems(error)

    # ESL_DATABASE_URL predates AD-007 and used to embed its password. The
    # gate, not the model, enforces the rule so unit fixtures stay simple and
    # the message can name the remedy without ever echoing the value.
    if urlsplit(settings.database_url).password:
        return None, (
            ConfigurationProblem(
                key="database_url",
                message=(
                    "must not embed a password; provision the state.password "
                    "key in the secret bundle instead (AD-017)"
                ),
            ),
        )
    # A mapping nobody can read would leave nobody authorized, which is safe
    # but silent; refuse readiness so the operator fixes it before the first
    # manual operation is refused for the wrong reason (FR-023).
    try:
        build_role_assignments(settings)
    except InvalidRoleAssignment as error:
        return None, (ConfigurationProblem(key="operator_roles", message=str(error)),)
    return settings, ()


def sanitized_configuration_snapshot(settings: Settings) -> dict[str, JSONValue]:
    """Return the secret-free, versioned snapshot of an active configuration.

    This is the content a ``configuration_version`` row records, so every
    execution can name the exact configuration it ran under (FR-002, FR-025).
    Rotating a credential does not change it, because secret-bearing settings
    are excluded entirely.
    """

    snapshot: dict[str, JSONValue] = {
        "configuration_schema_version": CONFIGURATION_SCHEMA_VERSION
    }
    for name in sorted(type(settings).model_fields):
        if name in SECRET_BEARING_SETTINGS:
            continue
        value = getattr(settings, name)
        snapshot[name] = value if isinstance(value, bool | int | str) else str(value)
    return snapshot


def configuration_content_hash(snapshot: Mapping[str, JSONValue]) -> str:
    """Return the deterministic content hash identifying a configuration."""

    return canonical_hash(_ConfigurationSnapshot(entries=tuple(sorted(snapshot.items()))))


@dataclass(frozen=True)
class _ConfigurationSnapshot:
    """Typed carrier so the canonical serializer keeps refusing raw mappings."""

    entries: tuple[tuple[str, JSONValue], ...]


def build_role_assignments(settings: Settings) -> dict[str, frozenset[Role]]:
    """Return who holds which role under this configuration (FR-023, AD-018).

    Lives with configuration for the same reason as ``build_retry_policy``:
    the domain parses the text but never reads ``Settings``.
    """

    return parse_role_assignments(settings.operator_roles)


def build_retry_policy(settings: Settings) -> RetryPolicy:
    """Build the retry policy from externalised configuration (FR-015, FR-025).

    This lives with configuration rather than with the policy itself so the
    domain stays free of ``Settings``: rules must be exercisable without an
    environment, and configuration may depend on the domain but never the
    reverse (FR-018).
    """

    return RetryPolicy(
        max_attempts=settings.retry_max_attempts,
        timeout_seconds=settings.retry_timeout_seconds,
        initial_backoff_seconds=settings.retry_initial_backoff_seconds,
        max_backoff_seconds=settings.retry_max_backoff_seconds,
        jitter_ratio=settings.retry_jitter_ratio,
    )
