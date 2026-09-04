"""Authorized read-only evidence for one workflow run (#109).

The persistence adapter supplies immutable rows that already passed storage
sanitization.  This boundary sanitizes evidence again and converts it to typed
views shared by the CLI, API, and metrics adapters.  No JSONB object is ever
returned directly.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from esl_service.application.recovery import (
    RecoveryReport,
    UncertainAction,
    recovery_report,
)
from esl_service.domain.authorization import Operation, Principal
from esl_service.domain.reconciliation import ReconciliationCounts
from esl_service.domain.serialization import JSONValue, sanitize_evidence

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000
CANONICALIZE_COUNT_KEYS = (
    "records",
    "extracted",
    "rejected",
    "unresolved",
    "issues",
)
RECONCILIATION_COUNT_KEYS = tuple(field.name for field in fields(ReconciliationCounts))


class EvidenceWithheld(RuntimeError):
    """Stored evidence carried a secret-like key, so the read refused to show it.

    The sanitizer fails closed by design (NFR-009); this names that outcome so
    the API answers with a fixed message and the CLI with an exit code, never
    a traceback, and never the offending value.
    """


@dataclass(frozen=True)
class IssueQuery:
    code: str | None = None
    severity: str | None = None
    item: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
        if self.offset < 0:
            raise ValueError("offset must not be negative")


@dataclass(frozen=True)
class ReportQuery:
    category: str | None = None
    item: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
        if self.offset < 0:
            raise ValueError("offset must not be negative")


@dataclass(frozen=True)
class IssueEvidenceRow:
    store_code: str
    item_code: str
    selling_uom: str | None
    rule_id: str
    issue_code: str
    severity: str
    evidence: Mapping[str, object]
    keyless: bool = False


@dataclass(frozen=True)
class IssueGroup:
    issue_code: str
    rule_id: str
    severity: str
    count: int


@dataclass(frozen=True)
class IssueSummary:
    """Grouped counts and the total under one filter, computed by the port."""

    groups: tuple[IssueGroup, ...]
    total: int


@dataclass(frozen=True)
class IssueDetail:
    store_code: str
    item_code: str
    selling_uom: str | None
    rule_id: str
    issue_code: str
    severity: str
    evidence: dict[str, JSONValue]
    keyless: bool


@dataclass(frozen=True)
class IssueRead:
    execution_id: UUID
    groups: tuple[IssueGroup, ...]
    records: tuple[IssueDetail, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ReconciliationExceptionRow:
    sequence: int
    category: str
    store_code: str | None
    item_code: str | None
    selling_uom: str | None
    expected_evidence: Mapping[str, object] | None
    actual_evidence: Mapping[str, object] | None
    resolution_status: str


@dataclass(frozen=True)
class ReconciliationReportRow:
    """The latest report revision's header and counts; exceptions are read by page."""

    report_id: UUID
    revision: int
    mode: str
    status: str
    generated_at: datetime
    finalized_at: datetime | None
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ExceptionGroup:
    category: str
    count: int


@dataclass(frozen=True)
class ExceptionSummary:
    groups: tuple[ExceptionGroup, ...]
    total: int


@dataclass(frozen=True)
class ExceptionDetail:
    sequence: int
    category: str
    store_code: str | None
    item_code: str | None
    selling_uom: str | None
    expected_evidence: dict[str, JSONValue] | None
    actual_evidence: dict[str, JSONValue] | None
    resolution_status: str


@dataclass(frozen=True)
class ReportRead:
    execution_id: UUID
    revision: int
    mode: str
    status: str
    generated_at: datetime
    finalized_at: datetime | None
    counts: dict[str, int]
    groups: tuple[ExceptionGroup, ...]
    exceptions: tuple[ExceptionDetail, ...]
    total: int
    limit: int
    offset: int


class EvidenceCheckpoint(Protocol):
    checkpoint_key: str
    watermark: str
    payload: Mapping[str, object]


class EvidenceStep(Protocol):
    step_name: str
    attempt: int
    outcome: str
    failure_class: str | None
    started_at: datetime
    ended_at: datetime | None
    checkpoints: Sequence[EvidenceCheckpoint]


class EvidenceExecution(Protocol):
    id: UUID
    workflow_name: str
    store_code: str
    status: str
    terminal_reason: str | None
    retry_not_before: datetime | None
    source_window_start: datetime
    source_window_end: datetime


@dataclass(frozen=True)
class RunEvidenceRows:
    execution: EvidenceExecution
    steps: Sequence[EvidenceStep]
    uncertain_actions: Sequence[UncertainAction]


@dataclass(frozen=True)
class StepRead:
    step_name: str
    attempt: int
    outcome: str
    failure_class: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    checkpoint_key: str | None
    checkpoint_watermark: str | None
    checkpoint_counts: dict[str, int]


@dataclass(frozen=True)
class RunDetailRead:
    execution: EvidenceExecution
    steps: tuple[StepRead, ...]
    recovery: RecoveryReport


@dataclass(frozen=True)
class MetricRunRow:
    """One recent run as the scrape sees it: counts only, no evidence rows."""

    workflow_name: str
    store_code: str
    issues: Mapping[str, int]
    reconciliation_counts: Mapping[str, int]
    steps: Sequence[EvidenceStep]


@dataclass(frozen=True)
class IssueMetric:
    workflow_name: str
    store_code: str
    issue_code: str
    count: int


@dataclass(frozen=True)
class ReconciliationMetric:
    workflow_name: str
    store_code: str
    count_name: str
    count: int


@dataclass(frozen=True)
class StepDurationMetric:
    workflow_name: str
    store_code: str
    step_name: str
    total_seconds: float
    sample_count: int


@dataclass(frozen=True)
class MetricsRead:
    run_limit_per_scope: int
    issues: tuple[IssueMetric, ...]
    reconciliation: tuple[ReconciliationMetric, ...]
    step_durations: tuple[StepDurationMetric, ...]


class EvidenceAuthorizer(Protocol):
    def authorize(
        self, principal: Principal, operation: Operation, *, resource_key: str
    ) -> object: ...


class RunEvidencePort(Protocol):
    """What the persistence adapter answers; every method is bounded at the query.

    A run can hold 15,000 records and 11,000 exceptions, so no method returns
    a whole run: summaries are grouped counts, pages are ``limit``/``offset``,
    and the metrics window carries counts per code, never evidence.
    """

    def execution_exists(self, execution_id: UUID) -> bool: ...

    def issue_summary_for(self, execution_id: UUID, query: IssueQuery) -> IssueSummary: ...

    def issue_page_for(
        self, execution_id: UUID, query: IssueQuery
    ) -> Sequence[IssueEvidenceRow]: ...

    def latest_report_for(
        self, execution_id: UUID
    ) -> ReconciliationReportRow | None: ...

    def exception_summary_for(self, report_id: UUID, query: ReportQuery) -> ExceptionSummary: ...

    def exception_page_for(
        self, report_id: UUID, query: ReportQuery
    ) -> Sequence[ReconciliationExceptionRow]: ...

    def run_evidence_for(self, execution_id: UUID) -> RunEvidenceRows: ...

    def metric_evidence(self, *, per_scope_limit: int) -> Sequence[MetricRunRow]: ...


class RunEvidenceService:
    """Authorize and assemble operator evidence without changing state."""

    def __init__(
        self,
        authorizer: EvidenceAuthorizer,
        evidence: RunEvidencePort,
        *,
        clock: Callable[[], datetime],
        metrics_run_limit: int,
    ) -> None:
        if metrics_run_limit <= 0:
            raise ValueError("metrics_run_limit must be positive")
        self._authorizer = authorizer
        self._evidence = evidence
        self._clock = clock
        self._metrics_run_limit = metrics_run_limit

    def issues(
        self,
        principal: Principal,
        execution_id: UUID,
        query: IssueQuery | None = None,
    ) -> IssueRead:
        self._authorize(principal, str(execution_id))
        self._require_execution(execution_id)
        effective = query or IssueQuery()
        summary = self._evidence.issue_summary_for(execution_id, effective)
        page = self._evidence.issue_page_for(execution_id, effective)
        return IssueRead(
            execution_id=execution_id,
            groups=tuple(summary.groups),
            records=tuple(_issue_detail(row) for row in page),
            total=summary.total,
            limit=effective.limit,
            offset=effective.offset,
        )

    def report(
        self,
        principal: Principal,
        execution_id: UUID,
        query: ReportQuery | None = None,
    ) -> ReportRead:
        self._authorize(principal, str(execution_id))
        self._require_execution(execution_id)
        report = self._evidence.latest_report_for(execution_id)
        if report is None:
            # Distinct from an unknown id: the run exists but has not reconciled.
            raise LookupError(f"execution {execution_id} has no reconciliation report yet")
        effective = query or ReportQuery()
        summary = self._evidence.exception_summary_for(report.report_id, effective)
        page = self._evidence.exception_page_for(report.report_id, effective)
        return ReportRead(
            execution_id=execution_id,
            revision=report.revision,
            mode=report.mode,
            status=report.status,
            generated_at=report.generated_at,
            finalized_at=report.finalized_at,
            counts={key: int(report.counts.get(key, 0)) for key in RECONCILIATION_COUNT_KEYS},
            groups=tuple(summary.groups),
            exceptions=tuple(_exception_detail(row) for row in page),
            total=summary.total,
            limit=effective.limit,
            offset=effective.offset,
        )

    def run_detail(self, principal: Principal, execution_id: UUID) -> RunDetailRead:
        self._authorize(principal, str(execution_id))
        rows = self._evidence.run_evidence_for(execution_id)
        steps = tuple(_step_read(step) for step in rows.steps)
        recovery = recovery_report(
            rows.execution,
            rows.steps,
            rows.uncertain_actions,
            now=self._clock(),
        )
        return RunDetailRead(rows.execution, steps, recovery)

    def metrics(self, principal: Principal) -> MetricsRead:
        self._authorize(principal, "metrics")
        rows = self._evidence.metric_evidence(per_scope_limit=self._metrics_run_limit)
        issues: Counter[tuple[str, str, str]] = Counter()
        reconciliation: Counter[tuple[str, str, str]] = Counter()
        duration_total: defaultdict[tuple[str, str, str], float] = defaultdict(float)
        duration_count: Counter[tuple[str, str, str]] = Counter()
        for run in rows:
            for issue_code, count in run.issues.items():
                issues[(run.workflow_name, run.store_code, issue_code)] += int(count)
            for name in RECONCILIATION_COUNT_KEYS:
                if name in run.reconciliation_counts:
                    reconciliation[(run.workflow_name, run.store_code, name)] += int(
                        run.reconciliation_counts[name]
                    )
            for step in run.steps:
                duration = _duration(step)
                if duration is None:
                    continue
                key = (run.workflow_name, run.store_code, step.step_name)
                duration_total[key] += duration
                duration_count[key] += 1
        return MetricsRead(
            run_limit_per_scope=self._metrics_run_limit,
            issues=tuple(IssueMetric(*key, count) for key, count in sorted(issues.items())),
            reconciliation=tuple(
                ReconciliationMetric(*key, count) for key, count in sorted(reconciliation.items())
            ),
            step_durations=tuple(
                StepDurationMetric(*key, duration_total[key], duration_count[key])
                for key in sorted(duration_total)
            ),
        )

    def _authorize(self, principal: Principal, resource_key: str) -> None:
        self._authorizer.authorize(principal, Operation.STATUS, resource_key=resource_key)

    def _require_execution(self, execution_id: UUID) -> None:
        if not self._evidence.execution_exists(execution_id):
            raise LookupError(f"no execution with id {execution_id}")


def _issue_detail(row: IssueEvidenceRow) -> IssueDetail:
    evidence = _safe_evidence(row.evidence)
    assert evidence is not None
    return IssueDetail(
        row.store_code,
        row.item_code,
        row.selling_uom,
        row.rule_id,
        row.issue_code,
        row.severity,
        evidence,
        row.keyless,
    )


def _exception_detail(row: ReconciliationExceptionRow) -> ExceptionDetail:
    return ExceptionDetail(
        sequence=row.sequence,
        category=row.category,
        store_code=row.store_code,
        item_code=row.item_code,
        selling_uom=row.selling_uom,
        expected_evidence=_safe_evidence(row.expected_evidence),
        actual_evidence=_safe_evidence(row.actual_evidence),
        resolution_status=row.resolution_status,
    )


def _safe_evidence(value: Mapping[str, object] | None) -> dict[str, JSONValue] | None:
    if value is None:
        return None
    try:
        sanitized = sanitize_evidence(cast("JSONValue", dict(value)))
    except ValueError as error:
        # The message names the key only, never the value (NFR-009).
        raise EvidenceWithheld(f"evidence withheld: {error}") from None
    if not isinstance(sanitized, dict):
        raise TypeError("operator evidence must be an object")
    return sanitized


def _duration(step: EvidenceStep) -> float | None:
    if step.ended_at is None:
        return None
    return max(0.0, (step.ended_at - step.started_at).total_seconds())


def _step_read(step: EvidenceStep) -> StepRead:
    checkpoint = step.checkpoints[-1] if step.checkpoints else None
    counts: dict[str, int] = {}
    if step.step_name == "canonicalize" and checkpoint is not None:
        for key in CANONICALIZE_COUNT_KEYS:
            value = checkpoint.payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[key] = value
    return StepRead(
        step_name=step.step_name,
        attempt=step.attempt,
        outcome=step.outcome,
        failure_class=step.failure_class,
        started_at=step.started_at,
        ended_at=step.ended_at,
        duration_seconds=_duration(step),
        checkpoint_key=None if checkpoint is None else checkpoint.checkpoint_key,
        checkpoint_watermark=None if checkpoint is None else checkpoint.watermark,
        checkpoint_counts=counts,
    )


__all__ = [
    "EvidenceWithheld",
    "ExceptionDetail",
    "ExceptionGroup",
    "ExceptionSummary",
    "IssueDetail",
    "IssueEvidenceRow",
    "IssueGroup",
    "IssueMetric",
    "IssueQuery",
    "IssueRead",
    "IssueSummary",
    "MetricRunRow",
    "MetricsRead",
    "ReconciliationExceptionRow",
    "ReconciliationMetric",
    "ReconciliationReportRow",
    "ReportQuery",
    "ReportRead",
    "RunDetailRead",
    "RunEvidencePort",
    "RunEvidenceRows",
    "RunEvidenceService",
    "StepDurationMetric",
    "StepRead",
]
