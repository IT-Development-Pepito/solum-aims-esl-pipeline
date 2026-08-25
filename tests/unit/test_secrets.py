import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from esl_service.config import Settings
from esl_service.runtime.secrets import DpapiSecretProvider, SecretUnavailableError


def test_dpapi_provider_returns_only_the_requested_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle_path = tmp_path / "secrets.dpapi"
    bundle_path.write_bytes(b"test-encrypted-bundle")
    monkeypatch.setitem(
        sys.modules,
        "win32crypt",
        SimpleNamespace(
            CryptUnprotectData=lambda *_: (
                "test bundle",
                b'{"database_password": "test-db-password", "api_token": "test-api-token"}',
            )
        ),
    )
    settings = Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql://state",
            "internal_host": "localhost",
            "secret_bundle_path": bundle_path,
        }
    )

    value = DpapiSecretProvider(settings).get("database_password")

    assert value == "test-db-password"


def test_dpapi_provider_does_not_expose_decryption_details_in_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle_path = tmp_path / "secrets.dpapi"
    bundle_path.write_bytes(b"test-encrypted-bundle")
    monkeypatch.setitem(
        sys.modules,
        "win32crypt",
        SimpleNamespace(
            CryptUnprotectData=lambda *_: (_ for _ in ()).throw(
                RuntimeError("test-secret-value")
            )
        ),
    )
    settings = Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql://state",
            "internal_host": "localhost",
            "secret_bundle_path": bundle_path,
        }
    )

    with pytest.raises(SecretUnavailableError) as error:
        DpapiSecretProvider(settings).get("database_password")

    assert "test-secret-value" not in str(error.value)
