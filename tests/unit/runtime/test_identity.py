"""Process identity check for bundle writes (#79).

Under user-scope DPAPI, a bundle written by the wrong Windows account is
undetectable until the service fails to read it, and that read error is
deliberately non-disclosing. So the writer checks who it is running as before
it writes, and the check has exactly three outcomes.
"""

from esl_service.runtime.identity import IdentityVerdict, check_identity


def test_matching_sid_is_a_match() -> None:
    verdict = check_identity(
        current_sid="S-1-5-21-1-2-3-1001", expected_sid="S-1-5-21-1-2-3-1001"
    )
    assert verdict is IdentityVerdict.MATCH


def test_sid_comparison_ignores_case() -> None:
    """Windows renders SIDs in upper case; a config value may not."""

    verdict = check_identity(
        current_sid="s-1-5-21-1-2-3-1001", expected_sid="S-1-5-21-1-2-3-1001"
    )
    assert verdict is IdentityVerdict.MATCH


def test_different_sid_is_a_mismatch() -> None:
    verdict = check_identity(
        current_sid="S-1-5-21-1-2-3-1001", expected_sid="S-1-5-21-1-2-3-2002"
    )
    assert verdict is IdentityVerdict.MISMATCH


def test_no_expected_sid_means_unconfigured_not_mismatch() -> None:
    """A development machine has no service account; that is not an error."""

    verdict = check_identity(current_sid="S-1-5-21-1-2-3-1001", expected_sid="")
    assert verdict is IdentityVerdict.UNCONFIGURED


def test_whitespace_only_expected_sid_is_unconfigured() -> None:
    verdict = check_identity(current_sid="S-1-5-21-1-2-3-1001", expected_sid="   ")
    assert verdict is IdentityVerdict.UNCONFIGURED
