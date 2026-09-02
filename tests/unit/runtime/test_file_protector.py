"""The real Windows file protector produces the ACL the validator accepts (#79).

Acceptance criterion: the written bundle grants access only to the configured
service identity and administrators, proven by a permission test. This test
protects a real file and reads its DACL back through the same reader the
production startup validator uses, so the writer and the validator cannot
drift apart unnoticed. Windows only, which is also where CI runs.
"""

import sys
from pathlib import Path

import pytest

from esl_service.config import WindowsSecretBundleAclReader
from esl_service.runtime.identity import current_process_sid
from esl_service.runtime.secrets import (
    ADMINISTRATORS_SID,
    LOCAL_SYSTEM_SID,
    WindowsFileProtector,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DACLs are Windows-only")


def test_the_protected_file_allows_only_the_approved_principals(tmp_path: Path) -> None:
    """Every ACE is ACCESS_ALLOWED, and only for the three approved SIDs."""

    target = tmp_path / "secrets.dpapi.tmp"
    target.write_bytes(b"placeholder")
    service_sid = current_process_sid()

    WindowsFileProtector().protect(target, service_sid)

    inspection = WindowsSecretBundleAclReader().read_file(target)
    approved = {service_sid, ADMINISTRATORS_SID, LOCAL_SYSTEM_SID}
    assert inspection.entries, "the DACL must not be empty"
    for entry in inspection.entries:
        assert entry.ace_type == 0, "only ACCESS_ALLOWED entries are permitted"
        assert entry.sid in approved, f"unexpected principal {entry.sid}"


def test_nothing_is_inherited_from_the_directory(tmp_path: Path) -> None:
    """A protected DACL discards the permissive entries a temp directory carries."""

    target = tmp_path / "secrets.dpapi.tmp"
    target.write_bytes(b"placeholder")
    before = {entry.sid for entry in WindowsSecretBundleAclReader().read_file(target).entries}

    WindowsFileProtector().protect(target, current_process_sid())

    after = {entry.sid for entry in WindowsSecretBundleAclReader().read_file(target).entries}
    assert after <= {current_process_sid(), ADMINISTRATORS_SID, LOCAL_SYSTEM_SID}
    assert not (before - after) <= set(), "the inherited principals were removed"


def test_without_a_service_sid_the_writer_keeps_its_own_access(tmp_path: Path) -> None:
    """A development bundle must stay readable by the developer who wrote it."""

    target = tmp_path / "secrets.dpapi.tmp"
    target.write_bytes(b"placeholder")

    WindowsFileProtector().protect(target, None)

    sids = {entry.sid for entry in WindowsSecretBundleAclReader().read_file(target).entries}
    assert current_process_sid() in sids
    assert target.read_bytes() == b"placeholder", "the writer can still read the file"
