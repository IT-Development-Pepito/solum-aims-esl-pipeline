"""Reconciliation balance rules (FR-021).

The formulas are the ones in SYSTEM_ARCHITECTURE.md section 5.7. They are
business/operational contracts: changing one requires updating the
specification, architecture, workflow, and these tests together.
"""

import pytest

from esl_service.domain.reconciliation import (
    ReconciliationCounts,
    ReconciliationMode,
    UnbalancedReconciliation,
    validate_balance,
)


def active_counts(**overrides: int) -> ReconciliationCounts:
    """Build a balanced ACTIVE terminal count set."""

    values: dict[str, int] = {
        "extracted": 10,
        "rejected": 1,
        "valid": 9,
        "ineligible": 2,
        "eligible": 7,
        "unchanged": 1,
        "skipped_idempotent": 1,
        "intended": 0,
        "acknowledged": 3,
        "rejected_by_aims": 1,
        "failed": 0,
        "unresolved": 1,
        "submitted": 0,
        "ambiguous": 0,
    }
    values.update(overrides)
    return ReconciliationCounts(**values)


def shadow_counts(**overrides: int) -> ReconciliationCounts:
    """Build a balanced SHADOW terminal count set."""

    values: dict[str, int] = {
        "extracted": 4,
        "rejected": 0,
        "valid": 4,
        "ineligible": 1,
        "eligible": 3,
        "unchanged": 1,
        "skipped_idempotent": 0,
        "intended": 1,
        "acknowledged": 0,
        "rejected_by_aims": 0,
        "failed": 0,
        "unresolved": 1,
        "submitted": 0,
        "ambiguous": 1,
    }
    values.update(overrides)
    return ReconciliationCounts(**values)


def test_active_terminal_balance() -> None:
    """The documented ACTIVE formula accepts a balanced report."""

    validate_balance(ReconciliationMode.ACTIVE, active_counts())


def test_shadow_terminal_balance() -> None:
    """A shadow run balances on intended rather than acknowledged."""

    validate_balance(ReconciliationMode.SHADOW, shadow_counts())


def test_transformation_balance_is_checked_first() -> None:
    """extracted = rejected + valid, before any action-stage arithmetic."""

    with pytest.raises(UnbalancedReconciliation, match="extracted"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(extracted=11))


def test_valid_balance_is_checked() -> None:
    """valid = ineligible + eligible."""

    with pytest.raises(UnbalancedReconciliation, match="valid"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(ineligible=3))


def test_submitted_blocks_a_terminal_report() -> None:
    """Submitted is in-flight, so it can never appear in a terminal report."""

    with pytest.raises(UnbalancedReconciliation, match="submitted"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(submitted=1))


def test_active_eligible_balance_is_checked() -> None:
    """An ACTIVE report must account for every eligible record."""

    with pytest.raises(UnbalancedReconciliation, match="eligible"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(acknowledged=2))


def test_shadow_report_may_not_acknowledge() -> None:
    """A shadow run causes no external effect, so it acknowledges nothing."""

    with pytest.raises(UnbalancedReconciliation, match="shadow"):
        validate_balance(
            ReconciliationMode.SHADOW,
            shadow_counts(acknowledged=1, intended=0),
        )


def test_ambiguous_is_a_diagnostic_subset_of_unresolved() -> None:
    """Ambiguity is counted inside unresolved and never added twice."""

    # Balanced: ambiguous is not a separate balancing term.
    validate_balance(ReconciliationMode.ACTIVE, active_counts(ambiguous=1))

    with pytest.raises(UnbalancedReconciliation, match="ambiguous"):
        validate_balance(ReconciliationMode.ACTIVE, active_counts(ambiguous=2))


def test_counts_may_not_be_negative() -> None:
    """A negative count is never a valid observation."""

    with pytest.raises(ValueError, match="must not be negative"):
        active_counts(failed=-1)


def test_unresolved_blocks_automatic_completion() -> None:
    """A non-zero unresolved count is reported, not silently accepted."""

    counts = active_counts()
    assert counts.unresolved == 1
    assert counts.blocks_completion() is True
    assert active_counts(unresolved=0, acknowledged=4).blocks_completion() is False
