"""Which Windows account this process runs as (#79, AD-007).

Under user-scope DPAPI a bundle written by the wrong account is undetectable
until the service fails to read it, and that read error is deliberately
non-disclosing. So the writer asks who it is before it writes. The check has
three outcomes rather than two, because a development machine has no service
account at all and that is not an error.
"""

import getpass
from enum import StrEnum
from typing import Any


class IdentityVerdict(StrEnum):
    """Outcome of comparing the running account with the configured one."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNCONFIGURED = "UNCONFIGURED"


def check_identity(*, current_sid: str, expected_sid: str) -> IdentityVerdict:
    """Compare SIDs case-insensitively; an empty expectation is unconfigured."""

    expected = expected_sid.strip()
    if not expected:
        return IdentityVerdict.UNCONFIGURED
    if current_sid.strip().upper() == expected.upper():
        return IdentityVerdict.MATCH
    return IdentityVerdict.MISMATCH


def _win32() -> tuple[Any, Any]:
    import win32api  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    return win32api, win32security


def current_process_sid() -> str:
    """Return the canonical string SID of the account running this process."""

    win32api, win32security = _win32()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    canonical: str = win32security.ConvertSidToStringSid(sid)
    return canonical.upper()


def current_user_name() -> str:
    """Return the account name for audit, without touching Windows APIs."""

    return getpass.getuser()
