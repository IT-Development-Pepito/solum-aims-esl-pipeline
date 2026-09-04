"""Read-only persistence adapter for operator run evidence (#109).

Every read is bounded where it runs. A run holds thousands of issues and
exceptions, so grouping, filtering, paging, and counting are SQL, never a
Python pass over the whole run; the metrics window reads counts per code and
the latest report's count columns, never an evidence row. Relational issues
and the keyless ``RECORD_EXCLUDED`` events are one ``UNION ALL`` source, so a
filter applies to both identically.
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import BigInteger, ColumnElement, false, func, select, true, union_all
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from esl_service.application.persist_run import EVENT_RECORD_EXCLUDED
from esl_service.application.recovery import UncertainAction
from esl_service.application.run_evidence import (
    RECONCILIATION_COUNT_KEYS,
    EvidenceStep,
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
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.models import (
    ExecutionEvent,
    ExecutionStep,
    ReconciliationException,
    ReconciliationReport,
    RecordIssue,
    RecordProcessingResult,
    WorkflowExecution,
)
from esl_service.persistence.repository import ExecutionRepository

#: A RECORD_EXCLUDED event must carry these text fields and an object
#: ``evidence`` to count as an issue; a malformed event is skipped, not fatal.
_EXCLUDED_TEXT_KEYS = ("store_code", "item_code", "rule_id", "issue_code", "severity")


class RunEvidenceRepository:
    """Query existing immutable rows without changing or recomputing them."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- existence --------------------------------------------------------------------

    def execution_exists(self, execution_id: UUID) -> bool:
        found = self._session.scalar(
            select(WorkflowExecution.id).where(WorkflowExecution.id == execution_id)
        )
        return found is not None

    # -- issues -----------------------------------------------------------------------

    def issue_summary_for(self, execution_id: UUID, query: IssueQuery) -> IssueSummary:
        source = _issue_source((execution_id,))
        count = func.count().label("count")
        rows = self._session.execute(
            select(source.c.issue_code, source.c.rule_id, source.c.severity, count)
            .where(*_issue_predicates(source, query))
            .group_by(source.c.issue_code, source.c.rule_id, source.c.severity)
            .order_by(count.desc(), source.c.issue_code, source.c.rule_id, source.c.severity)
        ).all()
        groups = tuple(
            IssueGroup(code, rule, severity, int(n)) for code, rule, severity, n in rows
        )
        return IssueSummary(groups=groups, total=sum(group.count for group in groups))

    def issue_page_for(self, execution_id: UUID, query: IssueQuery) -> Sequence[IssueEvidenceRow]:
        source = _issue_source((execution_id,))
        rows = self._session.execute(
            select(source)
            .where(*_issue_predicates(source, query))
            .order_by(
                source.c.store_code,
                source.c.item_code,
                func.coalesce(source.c.selling_uom, ""),
                source.c.issue_code,
                source.c.rule_id,
                source.c.keyless,
                source.c.ordinal,
            )
            .limit(query.limit)
            .offset(query.offset)
        ).all()
        return tuple(
            IssueEvidenceRow(
                store_code=row.store_code,
                item_code=row.item_code,
                selling_uom=row.selling_uom,
                rule_id=row.rule_id,
                issue_code=row.issue_code,
                severity=row.severity,
                evidence=cast("Mapping[str, object]", dict(row.evidence)),
                keyless=bool(row.keyless),
            )
            for row in rows
        )

    # -- reconciliation -----------------------------------------------------------------

    def latest_report_for(self, execution_id: UUID) -> ReconciliationReportRow | None:
        report = self._session.scalars(
            select(ReconciliationReport)
            .where(ReconciliationReport.execution_id == execution_id)
            .order_by(ReconciliationReport.revision.desc())
            .limit(1)
        ).first()
        if report is None:
            return None
        return ReconciliationReportRow(
            report_id=report.id,
            revision=report.revision,
            mode=report.mode,
            status=report.status,
            generated_at=report.generated_at,
            finalized_at=report.finalized_at,
            counts=_report_counts(report),
        )

    def exception_summary_for(self, report_id: UUID, query: ReportQuery) -> ExceptionSummary:
        count = func.count().label("count")
        rows = self._session.execute(
            select(ReconciliationException.category, count)
            .where(ReconciliationException.report_id == report_id, *_exception_predicates(query))
            .group_by(ReconciliationException.category)
            .order_by(count.desc(), ReconciliationException.category)
        ).all()
        groups = tuple(ExceptionGroup(category, int(n)) for category, n in rows)
        return ExceptionSummary(groups=groups, total=sum(group.count for group in groups))

    def exception_page_for(
        self, report_id: UUID, query: ReportQuery
    ) -> Sequence[ReconciliationExceptionRow]:
        rows = self._session.scalars(
            select(ReconciliationException)
            .where(ReconciliationException.report_id == report_id, *_exception_predicates(query))
            .order_by(ReconciliationException.sequence)
            .limit(query.limit)
            .offset(query.offset)
        )
        return tuple(
            ReconciliationExceptionRow(
                sequence=item.sequence,
                category=item.category,
                store_code=item.store_code,
                item_code=item.item_code,
                selling_uom=item.selling_uom,
                expected_evidence=_optional_mapping(item.expected_evidence),
                actual_evidence=_optional_mapping(item.actual_evidence),
                resolution_status=item.resolution_status,
            )
            for item in rows
        )

    # -- run detail -------------------------------------------------------------------

    def run_evidence_for(self, execution_id: UUID) -> RunEvidenceRows:
        executions = ExecutionRepository(self._session)
        execution = executions.get_execution(execution_id)
        actions = ActionRepository(self._session).unresolved_actions(execution_id=execution_id)
        return RunEvidenceRows(
            execution=execution,
            steps=cast("list[EvidenceStep]", executions.step_history(execution_id)),
            uncertain_actions=tuple(
                UncertainAction(item.id, item.idempotency_key, item.state) for item in actions
            ),
        )

    # -- metrics window ---------------------------------------------------------------

    def metric_evidence(self, *, per_scope_limit: int) -> tuple[MetricRunRow, ...]:
        """The newest ``per_scope_limit`` runs per workflow and store, as counts.

        Four bounded queries for the whole window: the ranked ids, the issue
        counts per run and code, the latest report's count columns per run,
        and the step rows without checkpoints. No evidence JSONB is read.
        """

        if per_scope_limit <= 0:
            raise ValueError("per_scope_limit must be positive")
        ranked = select(
            WorkflowExecution.id.label("execution_id"),
            WorkflowExecution.workflow_name.label("workflow_name"),
            WorkflowExecution.store_code.label("store_code"),
            func.row_number()
            .over(
                partition_by=(WorkflowExecution.workflow_name, WorkflowExecution.store_code),
                order_by=(WorkflowExecution.started_at.desc(), WorkflowExecution.id.desc()),
            )
            .label("scope_rank"),
        ).subquery("ranked")
        scopes = self._session.execute(
            select(ranked.c.execution_id, ranked.c.workflow_name, ranked.c.store_code)
            .where(ranked.c.scope_rank <= per_scope_limit)
            .order_by(ranked.c.execution_id)
        ).all()
        if not scopes:
            return ()
        ids = [row.execution_id for row in scopes]

        source = _issue_source(ids)
        issue_counts: dict[UUID, dict[str, int]] = {execution_id: {} for execution_id in ids}
        for execution_id, issue_code, n in self._session.execute(
            select(source.c.execution_id, source.c.issue_code, func.count()).group_by(
                source.c.execution_id, source.c.issue_code
            )
        ):
            issue_counts[execution_id][issue_code] = int(n)

        report_counts: dict[UUID, Mapping[str, int]] = {}
        for report in self._session.scalars(
            select(ReconciliationReport)
            .distinct(ReconciliationReport.execution_id)
            .where(ReconciliationReport.execution_id.in_(ids))
            .order_by(ReconciliationReport.execution_id, ReconciliationReport.revision.desc())
        ):
            report_counts[report.execution_id] = _report_counts(report)

        latest_steps: dict[UUID, dict[str, ExecutionStep]] = {
            execution_id: {} for execution_id in ids
        }
        for step in self._session.scalars(
            select(ExecutionStep)
            .where(ExecutionStep.execution_id.in_(ids))
            .order_by(ExecutionStep.sequence)
        ):
            current = latest_steps[step.execution_id].get(step.step_name)
            if current is None or step.attempt > current.attempt:
                latest_steps[step.execution_id][step.step_name] = step

        return tuple(
            MetricRunRow(
                workflow_name=row.workflow_name,
                store_code=row.store_code,
                issues=issue_counts[row.execution_id],
                reconciliation_counts=report_counts.get(row.execution_id, {}),
                steps=cast(
                    "list[EvidenceStep]",
                    sorted(latest_steps[row.execution_id].values(), key=lambda step: step.sequence),
                ),
            )
            for row in scopes
        )


# --- the shared issue source ---------------------------------------------------------


def _issue_source(execution_ids: Sequence[UUID]) -> Subquery:
    """Relational issues and well-formed keyless events as one row shape."""

    payload = ExecutionEvent.payload
    relational = (
        select(
            RecordProcessingResult.execution_id.label("execution_id"),
            RecordProcessingResult.store_code.label("store_code"),
            RecordProcessingResult.item_code.label("item_code"),
            RecordProcessingResult.selling_uom.label("selling_uom"),
            RecordIssue.rule_id.label("rule_id"),
            RecordIssue.issue_code.label("issue_code"),
            RecordIssue.severity.label("severity"),
            RecordIssue.evidence.label("evidence"),
            false().label("keyless"),
            sql_cast(RecordIssue.sequence, BigInteger).label("ordinal"),
        )
        .join(RecordProcessingResult, RecordIssue.result_id == RecordProcessingResult.id)
        .where(RecordProcessingResult.execution_id.in_(execution_ids))
    )
    events = select(
        ExecutionEvent.execution_id.label("execution_id"),
        payload["store_code"].astext.label("store_code"),
        payload["item_code"].astext.label("item_code"),
        payload["selling_uom"].astext.label("selling_uom"),
        payload["rule_id"].astext.label("rule_id"),
        payload["issue_code"].astext.label("issue_code"),
        payload["severity"].astext.label("severity"),
        payload["evidence"].label("evidence"),
        true().label("keyless"),
        ExecutionEvent.sequence.label("ordinal"),
    ).where(
        ExecutionEvent.execution_id.in_(execution_ids),
        ExecutionEvent.event_type == EVENT_RECORD_EXCLUDED,
        *(payload.has_key(key) for key in _EXCLUDED_TEXT_KEYS),
        func.jsonb_typeof(payload["evidence"]) == "object",
    )
    return union_all(relational, events).subquery("issue_source")


def _issue_predicates(source: Subquery, query: IssueQuery) -> list[ColumnElement[bool]]:
    predicates: list[ColumnElement[bool]] = []
    if query.code is not None:
        predicates.append(func.lower(source.c.issue_code) == query.code.casefold())
    if query.severity is not None:
        predicates.append(func.lower(source.c.severity) == query.severity.casefold())
    if query.item is not None:
        predicates.append(func.lower(source.c.item_code) == query.item.casefold())
    return predicates


def _exception_predicates(query: ReportQuery) -> list[ColumnElement[bool]]:
    predicates: list[ColumnElement[bool]] = []
    if query.category is not None:
        predicates.append(
            func.lower(ReconciliationException.category) == query.category.casefold()
        )
    if query.item is not None:
        predicates.append(
            func.lower(func.coalesce(ReconciliationException.item_code, "")) == query.item.casefold()
        )
    return predicates


def _report_counts(report: ReconciliationReport) -> dict[str, int]:
    return {name: int(getattr(report, name)) for name in RECONCILIATION_COUNT_KEYS}


def _optional_mapping(value: Any) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("evidence must be an object")
    return cast("Mapping[str, object]", dict(value))


__all__ = ["RunEvidenceRepository"]
