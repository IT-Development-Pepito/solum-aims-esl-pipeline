"""Windows DPAPI-backed runtime secret access."""

import json
from collections.abc import Mapping
from typing import Protocol

from esl_service.config import Settings


class SecretProvider(Protocol):
    """Provides one named secret without exposing a complete bundle."""

    def get(self, name: str) -> str: ...


class SecretUnavailableError(RuntimeError):
    """Raised without including sensitive bundle or decryption details."""


class DpapiSecretProvider:
    """Reads a configured DPAPI-protected JSON bundle on demand."""

    def __init__(self, settings: Settings) -> None:
        self._bundle_path = settings.secret_bundle_path

    def get(self, name: str) -> str:
        """Return one named value from the configured encrypted bundle."""
        try:
            encrypted_bundle = self._bundle_path.read_bytes()
            import win32crypt  # type: ignore[import-untyped]

            _, decrypted_bundle = win32crypt.CryptUnprotectData(
                encrypted_bundle, None, None, None, 0
            )
            values = json.loads(decrypted_bundle.decode("utf-8"))
        # Windows DPAPI exposes its native exception type only at runtime. Do not
        # propagate it because its message could disclose bundle details.
        except Exception:  # noqa: BLE001
            raise SecretUnavailableError("secret bundle is unavailable") from None

        if not isinstance(values, Mapping):
            raise SecretUnavailableError("secret bundle is unavailable")

        value = values.get(name)
        if not isinstance(value, str):
            raise SecretUnavailableError("requested secret is unavailable")
        return value
