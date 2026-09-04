"""An in-memory ``RunEvidencePort`` for the #109 service, API, and CLI tests.

The real repository pushes grouping, filtering, paging, and counting into
SQL; this fake does the same over tuples so the three test suites share one
port shape and none of them re-implements the contract in place.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from esl_service.application.run_evidence import (
    ExceptionGroup,
    ExceptionSummary,
    IssueEvidenceRow,
    IssueGroup,
    IssueQuery,
    IssueSummary,
    MetricRunRow,
    ReconciliationExceptionRow,
    ReconciliationReportRow,
    ReportQuery,
    RunEvidenceRows,
)


def _issue_matches(row: IssueEvidenceRow, query: IssueQuery) -> bool:
    return (
        (query.code is None or row.issue_code.casefold() == query.code.casefold())
        and (query.severity is None or row.severity.casefold() == query.severity.casefold())
        and (query.item is None or row.item_code.casefold() == query.item.casefold())
    )


def _exception_matches(row: ReconciliationExceptionRow, query: ReportQuery) -> bool:
    return (
        query.category is None or row.category.casefold() == query.category.casefold()
    ) and (query.item is None or (row.item_code or "").casefold() == query.item.casefold())


@dataclass
class FakeEvidencePort:
    """Rows a test seeds, served through the same port the repository implements."""

    known_executions: set[UUID] = field(default_factory=set)
    issues: tuple[IssueEvidenceRow, ...] = ()
    report: ReconciliationReportRow | None = None
    exceptions: tuple[ReconciliationExceptionRow, ...] = ()
    run_rows: dict[UUID, RunEvidenceRows] = field(default_factory=dict)
    metrics: tuple[MetricRunRow, ...] = ()
    metric_limit: int | None = None

    def execution_exists(self, execution_id: UUID) -> bool:
        return execution_id in self.known_executions or execution_id in self.run_rows

    def issue_summary_for(self, execution_id: UUID, query: IssueQuery) -> IssueSummary:
        selected = [row for row in self.issues if _issue_matches(row, query)]
        groups = Counter((row.issue_code, row.rule_id, row.severity) for row in selected)
        return IssueSummary(
            groups=tuple(
                IssueGroup(code, rule, severity, count)
                for (code, rule, severity), count in sorted(
                    groups.items(), key=lambda item: (-item[1], *item[0])
                )
            ),
            total=len(selected),
        )

    def issue_page_for(self, execution_id: UUID, query: IssueQuery) -> Sequence[IssueEvidenceRow]:
        selected = sorted(
            (row for row in self.issues if _issue_matches(row, query)),
            key=lambda row: (
                row.store_code, row.item_code, row.selling_uom or "", row.issue_code, row.rule_id, row.keyless,
            ),
        )
        return tuple(selected[query.offset : query.offset + query.limit])

    def latest_report_for(self, execution_id: UUID) -> ReconciliationReportRow | None:
        return self.report

    def exception_summary_for(self, report_id: UUID, query: ReportQuery) -> ExceptionSummary:
        selected = [row for row in self.exceptions if _exception_matches(row, query)]
        groups = Counter(row.category for row in selected)
        return ExceptionSummary(
            groups=tuple(
                ExceptionGroup(category, count)
                for category, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))
            ),
            total=len(selected),
        )

    def exception_page_for(
        self, report_id: UUID, query: ReportQuery
    ) -> Sequence[ReconciliationExceptionRow]:
        selected = sorted(
            (row for row in self.exceptions if _exception_matches(row, query)),
            key=lambda row: row.sequence,
        )
        return tuple(selected[query.offset : query.offset + query.limit])

    def run_evidence_for(self, execution_id: UUID) -> RunEvidenceRows:
        try:
            return self.run_rows[execution_id]
        except KeyError:
            raise LookupError(f"no execution with id {execution_id}") from None

    def metric_evidence(self, *, per_scope_limit: int) -> Sequence[MetricRunRow]:
        self.metric_limit = per_scope_limit
        return self.metrics


def issue_counts(rows: Sequence[IssueEvidenceRow]) -> Mapping[str, int]:
    """Counts per issue code, the shape ``MetricRunRow.issues`` carries."""

    return dict(Counter(row.issue_code for row in rows))
