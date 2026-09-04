"""Safe operator controls and status query boundaries (FR-011, FR-012).

This module is transport- and persistence-independent. Authorization belongs
to issue #26; these contracts establish that every retry/replay names an actor
and reason, a retry is eligible only from FAILED, an ambiguous external action
blocks retry, and replay/status ranges cannot silently become unbounded.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from esl_service.domain.workflow import ExecutionStatus

#: Refused controls are audit events even though no replacement run exists.
WORKFLOW_RETRY_REFUSED = "workflow.retry.refused"
WORKFLOW_SNAPSHOT_REPLAY_REFUSED = "workflow.snapshot_replay.refused"


class InvalidWorkflowControl(ValueError):
    """Raised when a retry or replay request violates its safety boundary."""


class InvalidExecutionQuery(ValueError):
    """Raised when a status query has no usable bounded selector."""


class RetryRefusalReason(StrEnum):
    """Stable reason codes for a failed-run retry refusal."""

    EXECUTION_NOT_FAILED = "EXECUTION_NOT_FAILED"
    UNRESOLVED_EXTERNAL_ACTION = "UNRESOLVED_EXTERNAL_ACTION"


@dataclass(frozen=True)
class RetryDecision:
    """Whether a stored execution may be used as a retry origin."""

    allowed: bool
    refusal: RetryRefusalReason | None

    def __post_init__(self) -> None:
        if self.allowed and self.refusal is not None:
            raise ValueError("an allowed retry has no refusal")
        if not self.allowed and self.refusal is None:
            raise ValueError("a refused retry must state a reason")


def _require_operator(requested_by: str, reason: str) -> None:
    for name, value in (("requested_by", requested_by), ("reason", reason)):
        if not value.strip():
            raise InvalidWorkflowControl(f"{name} must not be blank")


def _require_aware(moment: datetime, name: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvalidWorkflowControl(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class RetryRequest:
    """An operator request to create a linked retry (FR-011)."""

    requested_by: str
    reason: str

    def __post_init__(self) -> None:
        _require_operator(self.requested_by, self.reason)


@dataclass(frozen=True)
class ReplayRequest:
    """An operator request for one explicit source-window replay (FR-011)."""

    requested_by: str
    reason: str
    source_window_start: datetime | None
    source_window_end: datetime | None

    def __post_init__(self) -> None:
        _require_operator(self.requested_by, self.reason)
        if self.source_window_start is None or self.source_window_end is None:
            raise InvalidWorkflowControl("replay requires both source-window bounds")
        _require_aware(self.source_window_start, "source_window_start")
        _require_aware(self.source_window_end, "source_window_end")
        if self.source_window_start > self.source_window_end:
            raise InvalidWorkflowControl(
                "source_window_start must not follow source_window_end"
            )


def decide_retry(
    status: ExecutionStatus, *, has_unresolved_external_action: bool
) -> RetryDecision:
    """Allow only an unambiguous FAILED execution to become a retry origin."""

    if status is not ExecutionStatus.FAILED:
        return RetryDecision(False, RetryRefusalReason.EXECUTION_NOT_FAILED)
    if has_unresolved_external_action:
        return RetryDecision(False, RetryRefusalReason.UNRESOLVED_EXTERNAL_ACTION)
    return RetryDecision(True, None)


# --- snapshot replay (#114) ------------------------------------------------------


@dataclass(frozen=True)
class SnapshotReplayRequest:
    """An operator request to reproduce a run from its retained snapshot (#114).

    Unlike ``ReplayRequest`` it carries no window: the replay reads nothing
    live, so the original's window, configuration, and rule version apply.
    """

    requested_by: str
    reason: str

    def __post_init__(self) -> None:
        _require_operator(self.requested_by, self.reason)


class SnapshotReplayRefusalReason(StrEnum):
    """Stable reason codes for a refused snapshot replay."""

    #: The original's finalized SOURCE_EXPECTED capture no longer exists (purged, or never finalized).
    SNAPSHOT_EVIDENCE_MISSING = "SNAPSHOT_EVIDENCE_MISSING"
    #: The original's latest reconciliation report is absent or not finalized.
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"


@dataclass(frozen=True)
class SnapshotReplayDecision:
    allowed: bool
    refusal: SnapshotReplayRefusalReason | None

    def __post_init__(self) -> None:
        if self.allowed and self.refusal is not None:
            raise ValueError("an allowed snapshot replay has no refusal")
        if not self.allowed and self.refusal is None:
            raise ValueError("a refused snapshot replay must state a reason")


def decide_snapshot_replay(
    *, has_finalized_snapshot: bool, report_finalized: bool
) -> SnapshotReplayDecision:
    """Allow a replay only from retained, reconciled evidence.

    Raw source rows are not retained (AD-005), so the finalized canonical
    capture is the only input a replay can have; a run whose reconciliation
    is still open is not a settled origin to reproduce.
    """

    if not has_finalized_snapshot:
        return SnapshotReplayDecision(False, SnapshotReplayRefusalReason.SNAPSHOT_EVIDENCE_MISSING)
    if not report_finalized:
        return SnapshotReplayDecision(False, SnapshotReplayRefusalReason.RECONCILIATION_UNRESOLVED)
    return SnapshotReplayDecision(True, None)


@dataclass(frozen=True)
class ExecutionQuery:
    """Composable, bounded selectors for workflow status retrieval (FR-012)."""

    execution_id: UUID | None = None
    workflow_name: str | None = None
    store_code: str | None = None
    started_from: datetime | None = None
    started_to: datetime | None = None

    def __post_init__(self) -> None:
        if all(
            value is None
            for value in (
                self.execution_id,
                self.workflow_name,
                self.store_code,
                self.started_from,
                self.started_to,
            )
        ):
            raise InvalidExecutionQuery("at least one status selector is required")

        for name, value in (
            ("workflow_name", self.workflow_name),
            ("store_code", self.store_code),
        ):
            if value is not None and not value.strip():
                raise InvalidExecutionQuery(f"{name} must not be blank")

        if (self.started_from is None) != (self.started_to is None):
            raise InvalidExecutionQuery("a status time range requires both bounds")
        if self.started_from is None or self.started_to is None:
            return
        for name, moment in (
            ("started_from", self.started_from),
            ("started_to", self.started_to),
        ):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise InvalidExecutionQuery(f"{name} must be timezone-aware")
        if self.started_from > self.started_to:
            raise InvalidExecutionQuery("started_from must not follow started_to")
