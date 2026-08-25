"""Runtime configuration for the ESL operations service."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretBundlePathValidator(Protocol):
    """Checks the Windows-specific security boundary for a secret bundle."""

    def validate(self, path: Path) -> None: ...


class WindowsSecretBundlePathValidator:
    """Reject missing bundles and DACLs that grant broad user groups access."""

    _BROAD_ACCESS_SIDS = frozenset(
        {
            "S-1-1-0",  # Everyone
            "S-1-5-11",  # Authenticated Users
            "S-1-5-32-545",  # BUILTIN\\Users
            "S-1-5-32-547",  # BUILTIN\\Power Users
        }
    )
    _ALLOW_ACE_TYPES = frozenset({0, 5})

    def validate(self, path: Path) -> None:
        if not path.is_file():
            raise ValueError("secret_bundle_path must reference an existing bundle")

        try:
            import win32security  # type: ignore[import-untyped]

            descriptor = win32security.GetFileSecurity(
                str(path), win32security.DACL_SECURITY_INFORMATION
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                raise ValueError("secret_bundle_path ACL is missing")

            for index in range(dacl.GetAceCount()):
                header, access_mask, sid = dacl.GetAce(index)
                if (
                    header[0] in self._ALLOW_ACE_TYPES
                    and access_mask
                    and win32security.ConvertSidToStringSid(sid)
                    in self._BROAD_ACCESS_SIDS
                ):
                    raise ValueError("secret_bundle_path ACL grants broad access")
        except ValueError:
            raise
        except Exception:  # noqa: BLE001
            raise ValueError("secret_bundle_path ACL could not be verified") from None


class Settings(BaseSettings):
    """Configuration loaded from explicit values or ``ESL_`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="ESL_", extra="forbid")

    environment: Literal["development", "staging", "production"]
    database_url: str
    internal_host: str
    shadow_mode: bool = True
    secret_bundle_path: Path = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")
    secret_bundle_path_validator_factory: ClassVar[
        Callable[[], SecretBundlePathValidator]
    ] = WindowsSecretBundlePathValidator

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment == "production" and not self.internal_host.strip():
            raise ValueError("internal_host must be configured for production")
        if self.environment != "production":
            return self

        canonical_bundle_path = self.secret_bundle_path.resolve(strict=False)
        approved_bundle_path = (
            Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            / "SOLUM"
            / "ESL"
            / "secrets.dpapi"
        ).resolve(strict=False)
        if (
            not self.secret_bundle_path.is_absolute()
            or canonical_bundle_path != approved_bundle_path
        ):
            raise ValueError(
                "secret_bundle_path must use the approved ProgramData location"
            )

        validator = type(self).secret_bundle_path_validator_factory()
        validator.validate(canonical_bundle_path)
        return self
