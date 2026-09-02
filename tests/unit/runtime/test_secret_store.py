"""Writing the DPAPI secret bundle (#79, NFR-009, AD-007).

The repository had a reader and no writer. These tests pin the writer's
guarantees: a secret set here is returned unchanged by the existing reader, the
plaintext never touches disk, an unreadable bundle is never clobbered, and the
file's permissions are applied before the bundle becomes visible at its final
path. Real DPAPI is exercised only where Windows is available; everything else
uses a codec that obfuscates but does not encrypt, so the logic is testable
anywhere.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

from esl_service.config import Settings
from esl_service.runtime.secrets import (
    DpapiBundleCodec,
    DpapiSecretProvider,
    InvalidSecretName,
    SecretBundleStore,
    SecretUnavailableError,
)


class Base64Codec:
    """Obfuscates so a test can prove the value is not stored verbatim."""

    def protect(self, data: bytes) -> bytes:
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        return base64.b64decode(data)


class RecordingProtector:
    """Records the path it was asked to protect and whether it existed then."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str | None, bool]] = []

    def protect(self, path: Path, service_identity_sid: str | None) -> None:
        self.calls.append((path, service_identity_sid, path.exists()))


def store(tmp_path: Path, sid: str | None = None) -> SecretBundleStore:
    """Build a store on a temporary bundle path with the test codec."""

    return SecretBundleStore(
        tmp_path / "secrets.dpapi",
        codec=Base64Codec(),
        protector=RecordingProtector(),
        service_identity_sid=sid,
    )


def settings_for(bundle: Path) -> Settings:
    return Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql://state",
            "internal_host": "localhost",
            "secret_bundle_path": bundle,
        }
    )


# --- round trip through the existing reader (acceptance criterion 1) -------


def test_a_secret_set_by_the_store_is_returned_by_the_reader(tmp_path: Path) -> None:
    """The writer and the existing reader agree on the bundle format."""

    bundle = store(tmp_path)
    bundle.set("source.sql.password", "s3cret-value")

    provider = DpapiSecretProvider(settings_for(bundle.path), codec=Base64Codec())

    assert provider.get("source.sql.password") == "s3cret-value"


def test_setting_a_second_secret_keeps_the_first(tmp_path: Path) -> None:
    """A bundle is a set of named secrets, not a single value."""

    bundle = store(tmp_path)
    bundle.set("first", "one")
    bundle.set("second", "two")

    assert bundle.keys() == ("first", "second")


def test_setting_an_existing_name_replaces_its_value(tmp_path: Path) -> None:
    """Rotation is a set on the same name."""

    bundle = store(tmp_path)
    bundle.set("k", "old")
    bundle.set("k", "new")

    provider = DpapiSecretProvider(settings_for(bundle.path), codec=Base64Codec())
    assert provider.get("k") == "new"


# --- the value never touches disk in the clear (acceptance criterion 2) ----


def test_the_plaintext_value_is_not_written_to_disk(tmp_path: Path) -> None:
    """Whatever the codec does, the raw value must not be findable in the file."""

    bundle = store(tmp_path)
    bundle.set("k", "needle-9f8e7d")

    on_disk = bundle.path.read_bytes()
    assert b"needle-9f8e7d" not in on_disk
    assert not list(tmp_path.glob("*.tmp")), "no temporary file may be left behind"


def test_keys_are_listable_without_values(tmp_path: Path) -> None:
    """Listing is a diagnostic; it must never be a disclosure."""

    bundle = store(tmp_path)
    bundle.set("k", "value")

    assert bundle.keys() == ("k",)
    assert "value" not in repr(bundle.keys())


# --- removal ---------------------------------------------------------------


def test_removing_a_secret_reports_whether_it_existed(tmp_path: Path) -> None:
    bundle = store(tmp_path)
    bundle.set("k", "v")

    assert bundle.remove("k") is True
    assert bundle.remove("k") is False
    assert bundle.keys() == ()


# --- safety: never clobber, never accept garbage ---------------------------


def test_an_unreadable_existing_bundle_is_not_overwritten(tmp_path: Path) -> None:
    """Refusing beats silently discarding every other secret in the bundle."""

    bundle = store(tmp_path)
    bundle.path.write_bytes(b"this is not base64 json \xff")

    with pytest.raises(SecretUnavailableError):
        bundle.set("k", "v")

    assert bundle.path.read_bytes() == b"this is not base64 json \xff"


def test_a_missing_bundle_is_created_on_first_set(tmp_path: Path) -> None:
    bundle = store(tmp_path)
    assert not bundle.path.exists()

    bundle.set("k", "v")

    assert bundle.path.exists()


@pytest.mark.parametrize("name", ["", "  ", "has space", "bad/slash", "semi;colon"])
def test_a_secret_name_must_be_a_plain_identifier(tmp_path: Path, name: str) -> None:
    """Names appear in audit and on screen, so they stay unambiguous."""

    with pytest.raises(InvalidSecretName):
        store(tmp_path).set(name, "v")


def test_an_empty_value_is_refused(tmp_path: Path) -> None:
    """An empty secret is almost always a paste error, never an intent."""

    with pytest.raises(ValueError, match="empty"):
        store(tmp_path).set("k", "")


# --- permissions are applied before the bundle is visible (criterion 3) ----


def test_the_file_is_protected_before_it_reaches_its_final_path(tmp_path: Path) -> None:
    """The ACL goes on the temporary file, so the final file is never exposed."""

    protector = RecordingProtector()
    bundle = SecretBundleStore(
        tmp_path / "secrets.dpapi",
        codec=Base64Codec(),
        protector=protector,
        service_identity_sid="S-1-5-21-1-2-3-1001",
    )

    bundle.set("k", "v")

    assert len(protector.calls) == 1
    protected_path, sid, existed = protector.calls[0]
    assert protected_path != bundle.path, "protection must target the temp file"
    assert protected_path.parent == bundle.path.parent
    assert existed is True, "the temp file must exist when protected"
    assert sid == "S-1-5-21-1-2-3-1001"


def test_the_bundle_stays_json_of_string_values(tmp_path: Path) -> None:
    """The reader rejects non-string values, so the writer never produces them."""

    bundle = store(tmp_path)
    bundle.set("k", "v")

    decoded = json.loads(base64.b64decode(bundle.path.read_bytes()))
    assert decoded == {"k": "v"}


# --- real DPAPI, user scope (Windows only) ---------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_codec_round_trips_and_does_not_store_plaintext() -> None:
    """The real codec encrypts under user scope and decrypts for the same user."""

    codec = DpapiBundleCodec()
    protected = codec.protect(b"needle-1a2b3c")

    assert b"needle-1a2b3c" not in protected
    assert codec.unprotect(protected) == b"needle-1a2b3c"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_store_round_trips_through_the_real_reader(tmp_path: Path) -> None:
    """End to end with real encryption: set with the store, get with the reader."""

    bundle = SecretBundleStore(
        tmp_path / "secrets.dpapi",
        codec=DpapiBundleCodec(),
        protector=RecordingProtector(),
        service_identity_sid=None,
    )
    bundle.set("aims.portal.password", "real-dpapi-value")

    assert DpapiSecretProvider(settings_for(bundle.path)).get("aims.portal.password") == (
        "real-dpapi-value"
    )
