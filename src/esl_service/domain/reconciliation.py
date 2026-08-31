"""Reconciliation balance rules for one execution (FR-021).

The formulas below are exactly those in ``docs/SYSTEM_ARCHITECTURE.md``
section 5.7. They are business and operational contracts: changing one is a
change to the specification, architecture, workflow, and tests together, not
an implementation detail.

Two rules carry most of the weight:

* ``submitted`` is an in-flight observation and can never appear in a terminal
  report. An execution with a lingering submission becomes OUTCOME_UNKNOWN and
  is counted as unresolved instead.
* ``ambiguous`` is a **diagnostic subset of unresolved**, not an additional
  balancing category, so it is never added twice.
"""

from dataclasses import dataclass, fields
from enum import StrEnum


class ReconciliationMode(StrEnum):
    """Whether the execution being reconciled could cause an external effect."""

    ACTIVE = "ACTIVE"
    SHADOW = "SHADOW"


class UnbalancedReconciliation(ValueError):
    """Raised when a report does not satisfy a documented balance rule."""


@dataclass(frozen=True)
class ReconciliationCounts:
    """Every count a reconciliation report must balance and enumerate."""

    extracted: int
    rejected: int
    valid: int
    ineligible: int
    eligible: int
    unchanged: int
    skipped_idempotent: int
    intended: int
    acknowledged: int
    rejected_by_aims: int
    failed: int
    unresolved: int
    submitted: int
    ambiguous: int

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) < 0:
                raise ValueError(f"{field.name} must not be negative")

    def blocks_completion(self) -> bool:
        """Return whether unresolved work blocks automatic completion."""

        return self.unresolved > 0


def validate_balance(
    mode: ReconciliationMode, counts: ReconciliationCounts
) -> None:
    """Raise unless the counts satisfy the documented balance for the mode."""

    if counts.extracted != counts.rejected + counts.valid:
        raise UnbalancedReconciliation(
            "extracted must equal rejected + valid: "
            f"{counts.extracted} != {counts.rejected} + {counts.valid}"
        )
    if counts.valid != counts.ineligible + counts.eligible:
        raise UnbalancedReconciliation(
            "valid must equal ineligible + eligible: "
            f"{counts.valid} != {counts.ineligible} + {counts.eligible}"
        )
    if counts.submitted:
        raise UnbalancedReconciliation(
            "submitted actions are not terminal: an execution with a lingering "
            "submission is OUTCOME_UNKNOWN and counted as unresolved"
        )
    if counts.ambiguous > counts.unresolved:
        raise UnbalancedReconciliation(
            "ambiguous is a diagnostic subset of unresolved: "
            f"{counts.ambiguous} > {counts.unresolved}"
        )

    if mode is ReconciliationMode.ACTIVE:
        terminal = (
            counts.unchanged
            + counts.skipped_idempotent
            + counts.acknowledged
            + counts.rejected_by_aims
            + counts.failed
            + counts.unresolved
        )
    else:
        if counts.acknowledged or counts.rejected_by_aims or counts.failed:
            raise UnbalancedReconciliation(
                "a shadow execution causes no external effect, so it cannot "
                "acknowledge, be rejected by AIMS, or fail a submission"
            )
        terminal = (
            counts.unchanged
            + counts.skipped_idempotent
            + counts.intended
            + counts.unresolved
        )

    if counts.eligible != terminal:
        raise UnbalancedReconciliation(
            f"eligible must equal the {mode.value} terminal categories: "
            f"{counts.eligible} != {terminal}"
        )
