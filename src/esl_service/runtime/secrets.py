"""Windows DPAPI-backed runtime secret access and provisioning (NFR-009, AD-007).

The bundle is one file: a JSON object of named string values, encrypted with
DPAPI under **user scope**. Only the Windows account that encrypted it can
decrypt it, so the bundle must be written by the service account itself. That
is the tighter of DPAPI's two scopes and the one the owner approved; machine
scope would let any process on the host decrypt, leaving the file ACL as the
only control.

Two consequences follow and are enforced here rather than left to memory. The
plaintext never reaches disk: the bundle is encoded in memory and written to a
temporary file that receives its ACL *before* it is renamed into place, so the
final path is never briefly world-readable. And an existing bundle that cannot
be read is never overwritten, because a single bad write would silently discard
every other secret in it.

Every failure raised from this module is deliberately non-disclosing. A DPAPI
error, a JSON error, or a file error is reported as "unavailable" without its
original text, because that text can carry bundle contents or decryption
detail.
"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from esl_service.config import ConfigurationProblem, Settings

#: Names appear in audit entries and on screen, so they stay unambiguous.
SECRET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Local administrators, always permitted so the bundle can be recovered.
ADMINISTRATORS_SID = "S-1-5-32-544"
#: LocalSystem, permitted because the Windows Service may run as it.
LOCAL_SYSTEM_SID = "S-1-5-18"


class SecretProvider(Protocol):
    """Provides one named secret without exposing a complete bundle."""

    def get(self, name: str) -> str: ...


class SecretUnavailableError(RuntimeError):
    """Raised without including sensitive bundle or decryption details."""


class InvalidSecretName(ValueError):
    """Raised when a secret name would be ambiguous in audit or on screen."""


# --- encryption ------------------------------------------------------------


class BundleCodec(Protocol):
    """Encrypts and decrypts the serialized bundle."""

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


def _win32crypt() -> Any:
    import win32crypt  # type: ignore[import-untyped]

    return win32crypt


class DpapiBundleCodec:
    """DPAPI under user scope: flags are zero on both sides, so the scope is
    fixed at encryption time and only the encrypting account can decrypt."""

    def protect(self, data: bytes) -> bytes:
        protected: bytes = _win32crypt().CryptProtectData(data, None, None, None, None, 0)
        return protected

    def unprotect(self, data: bytes) -> bytes:
        _, plain = _win32crypt().CryptUnprotectData(data, None, None, None, 0)
        unprotected: bytes = plain
        return unprotected


# --- file permissions ------------------------------------------------------


class FileProtector(Protocol):
    """Applies the bundle's access control before it becomes visible."""

    def protect(self, path: Path, service_identity_sid: str | None) -> None: ...


class WindowsFileProtector:
    """Replaces the DACL with exactly the principals the startup validator accepts.

    The validator in :mod:`esl_service.config` allows only the service SID,
    local administrators, and SYSTEM, and only ``ACCESS_ALLOWED`` entries. The
    DACL is written as protected so nothing is inherited from the directory.
    When no service SID is configured, the current account is used instead, so
    a development bundle stays readable by the developer who wrote it.
    """

    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        import ntsecuritycon  # type: ignore[import-untyped]
        import win32security  # type: ignore[import-untyped]

        from esl_service.runtime.identity import current_process_sid

        principals = [ADMINISTRATORS_SID, LOCAL_SYSTEM_SID]
        principals.append(service_identity_sid or current_process_sid())

        dacl = win32security.ACL()
        for sid in principals:
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION,
                ntsecuritycon.FILE_ALL_ACCESS,
                win32security.ConvertStringSidToSid(sid),
            )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )


# --- reading ---------------------------------------------------------------


def _load_bundle(path: Path, codec: BundleCodec) -> dict[str, str]:
    """Decrypt and parse the bundle, or raise without disclosing why."""

    try:
        values = json.loads(codec.unprotect(path.read_bytes()).decode("utf-8"))
    # DPAPI exposes its native exception type only at run time, and any of
    # these errors could carry bundle contents in its message.
    except Exception:  # noqa: BLE001
        raise SecretUnavailableError("secret bundle is unavailable") from None

    if not isinstance(values, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise SecretUnavailableError("secret bundle is unavailable")
    return dict(values)


class BundleSecretProvider:
    """Reads one named value from a bundle at a known path."""

    def __init__(self, path: Path, codec: BundleCodec | None = None) -> None:
        self._path = path
        self._codec = codec or DpapiBundleCodec()

    def get(self, name: str) -> str:
        """Return one named value from the configured encrypted bundle."""

        value = _load_bundle(self._path, self._codec).get(name)
        if value is None:
            raise SecretUnavailableError("requested secret is unavailable")
        return value


class DpapiSecretProvider(BundleSecretProvider):
    """Reads the bundle the configuration points at, on demand."""

    def __init__(self, settings: Settings, codec: BundleCodec | None = None) -> None:
        super().__init__(settings.secret_bundle_path, codec)


# --- writing ---------------------------------------------------------------


class SecretBundleStore:
    """Creates and updates the bundle without ever exposing its contents."""

    def __init__(
        self,
        path: Path,
        *,
        codec: BundleCodec,
        protector: FileProtector,
        service_identity_sid: str | None,
    ) -> None:
        self._path = path
        self._codec = codec
        self._protector = protector
        self._service_identity_sid = service_identity_sid

    @property
    def path(self) -> Path:
        return self._path

    def keys(self) -> tuple[str, ...]:
        """Return the names the bundle holds, never their values."""

        return tuple(sorted(_load_bundle(self._path, self._codec)))

    def set(self, name: str, value: str) -> None:
        """Store or replace one named secret."""

        if not SECRET_NAME_PATTERN.match(name):
            raise InvalidSecretName(
                "secret name must be letters, digits, dots, dashes, or underscores"
            )
        if not value:
            raise ValueError("secret value must not be empty")

        values = self._read_or_empty()
        values[name] = value
        self._write(values)

    def remove(self, name: str) -> bool:
        """Remove one named secret, reporting whether it was present."""

        values = self._read_or_empty()
        if name not in values:
            return False
        del values[name]
        self._write(values)
        return True

    def _read_or_empty(self) -> dict[str, str]:
        # A missing bundle is the normal first-run state. An existing one that
        # cannot be read is refused, so a bad write never discards its contents.
        if not self._path.exists():
            return {}
        return _load_bundle(self._path, self._codec)

    def _write(self, values: dict[str, str]) -> None:
        payload = self._codec.protect(
            json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        try:
            temporary.write_bytes(payload)
            # The ACL goes on the temporary file so the final path is never
            # visible with permissive permissions, even briefly.
            self._protector.protect(temporary, self._service_identity_sid)
            os.replace(temporary, self._path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


# --- fixed bundle keys (#78) -------------------------------------------------

#: The service's own PostgreSQL state store.
STATE_PASSWORD_KEY = "state.password"
#: One read-only account covers every SQL Server tier, confirmed by the owner.
SOURCE_SQL_PASSWORD_KEY = "source.sql.password"
AIMS_PORTAL_PASSWORD_KEY = "aims.portal.password"
AIMS_CORE_PASSWORD_KEY = "aims.core.password"

#: Fixed wording for a missing secret. Never the provider's own message, which
#: may name the bundle path.
SECRET_PROBLEM_MESSAGE = "secret is unavailable in the bundle"


def describe_secret_problems(
    settings: Settings, provider: SecretProvider
) -> tuple[ConfigurationProblem, ...]:
    """Name every configured target whose bundle key cannot be read (FR-025).

    Mirrors ``describe_configuration_problems``: one problem per key, the key
    only, and a fixed message. A shared key is reported once even though three
    tiers use it, and an unconfigured tier is not checked because it has
    nothing to read.
    """

    # Imported here because connectivity imports this module for its types.
    from esl_service.runtime.connectivity import targets_from_settings

    keys: list[str] = []
    for target in targets_from_settings(settings):
        if target.configured() and target.password_key not in keys:
            keys.append(target.password_key)

    problems: list[ConfigurationProblem] = []
    for key in keys:
        try:
            provider.get(key)
        except SecretUnavailableError:
            problems.append(
                ConfigurationProblem(key=f"secret.{key}", message=SECRET_PROBLEM_MESSAGE)
            )
    return tuple(problems)
