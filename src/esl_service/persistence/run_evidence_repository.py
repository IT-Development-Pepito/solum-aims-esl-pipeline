"""Read-only persistence adapter for operator run evidence (#109)."""

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.application.persist_run import EVENT_RECORD_EXCLUDED
from esl_service.application.recovery import UncertainAction
from esl_service.application.run_evidence import (
    RECONCILIATION_COUNT_KEYS,
    EvidenceStep,
    IssueEvidenceRow,
    MetricRunRow,
    ReconciliationExceptionRow,
    ReconciliationReportRow,
    RunEvidenceRows,
)
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.models import (
    ExecutionEvent,
    ReconciliationException,
    ReconciliationReport,
    RecordIssue,
    RecordProcessingResult,
    WorkflowExecution,
)
from esl_service.persistence.repository import ExecutionRepository


class RunEvidenceRepository:
    """Query existing immutable rows without changing or recomputing them."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def issues_for(self, execution_id: UUID) -> tuple[IssueEvidenceRow, ...]:
        relational = self._session.execute(
            select(RecordIssue, RecordProcessingResult)
            .join(
                RecordProcessingResult,
                RecordIssue.result_id == RecordProcessingResult.id,
            )
            .where(RecordProcessingResult.execution_id == execution_id)
            .order_by(
                RecordProcessingResult.store_code,
                RecordProcessingResult.item_code,
                RecordProcessingResult.selling_uom,
                RecordIssue.sequence,
            )
        ).all()
        rows = [
            IssueEvidenceRow(
                store_code=result.store_code,
                item_code=result.item_code,
                selling_uom=result.selling_uom,
                rule_id=issue.rule_id,
                issue_code=issue.issue_code,
                severity=issue.severity,
                evidence=dict(issue.evidence),
            )
            for issue, result in relational
        ]
        events = self._session.scalars(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.execution_id == execution_id,
                ExecutionEvent.event_type == EVENT_RECORD_EXCLUDED,
            )
            .order_by(ExecutionEvent.sequence)
        )
        rows.extend(_excluded_issue(event) for event in events)
        return tuple(rows)

    def latest_report_for(
        self, execution_id: UUID
    ) -> ReconciliationReportRow | None:
        report = self._session.scalars(
            select(ReconciliationReport)
            .where(ReconciliationReport.execution_id == execution_id)
            .order_by(ReconciliationReport.revision.desc())
        ).first()
        if report is None:
            return None
        exceptions = self._session.scalars(
            select(ReconciliationException)
            .where(ReconciliationException.report_id == report.id)
            .order_by(ReconciliationException.sequence)
        )
        return ReconciliationReportRow(
            revision=report.revision,
            mode=report.mode,
            status=report.status,
            generated_at=report.generated_at,
            finalized_at=report.finalized_at,
            counts={name: int(getattr(report, name)) for name in RECONCILIATION_COUNT_KEYS},
            exceptions=tuple(
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
                for item in exceptions
            ),
        )

    def run_evidence_for(self, execution_id: UUID) -> RunEvidenceRows:
        executions = ExecutionRepository(self._session)
        execution = executions.get_execution(execution_id)
        actions = ActionRepository(self._session).unresolved_actions(
            execution_id=execution_id
        )
        return RunEvidenceRows(
            execution=execution,
            steps=cast(
                "list[EvidenceStep]", executions.step_history(execution_id)
            ),
            uncertain_actions=tuple(
                UncertainAction(item.id, item.idempotency_key, item.state)
                for item in actions
            ),
        )

    def metric_evidence(self, *, per_scope_limit: int) -> tuple[MetricRunRow, ...]:
        if per_scope_limit <= 0:
            raise ValueError("per_scope_limit must be positive")
        ranked = select(
            WorkflowExecution.id.label("execution_id"),
            func.row_number()
            .over(
                partition_by=(
                    WorkflowExecution.workflow_name,
                    WorkflowExecution.store_code,
                ),
                order_by=(
                    WorkflowExecution.started_at.desc(),
                    WorkflowExecution.id.desc(),
                ),
            )
            .label("scope_rank"),
        ).subquery()
        ids = self._session.scalars(
            select(ranked.c.execution_id)
            .where(ranked.c.scope_rank <= per_scope_limit)
            .order_by(ranked.c.execution_id)
        ).all()
        if not ids:
            return ()
        executions = {
            item.id: item
            for item in self._session.scalars(
                select(WorkflowExecution).where(WorkflowExecution.id.in_(ids))
            )
        }
        step_repository = ExecutionRepository(self._session)
        rows: list[MetricRunRow] = []
        for execution_id in ids:
            execution = executions[execution_id]
            report = self.latest_report_for(execution_id)
            rows.append(
                MetricRunRow(
                    workflow_name=execution.workflow_name,
                    store_code=execution.store_code,
                    issues=self.issues_for(execution_id),
                    reconciliation_counts={} if report is None else report.counts,
                    steps=cast(
                        "list[EvidenceStep]",
                        step_repository.step_history(execution_id),
                    ),
                )
            )
        return tuple(rows)


def _excluded_issue(event: ExecutionEvent) -> IssueEvidenceRow:
    payload = event.payload
    try:
        store_code = _required_text(payload, "store_code")
        item_code = _required_text(payload, "item_code")
        rule_id = _required_text(payload, "rule_id")
        issue_code = _required_text(payload, "issue_code")
        severity = _required_text(payload, "severity")
        selling_uom = _optional_text(payload.get("selling_uom"))
        evidence = payload["evidence"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"malformed {EVENT_RECORD_EXCLUDED} event sequence {event.sequence}"
        ) from error
    if not isinstance(evidence, dict):
        raise TypeError(
            f"malformed {EVENT_RECORD_EXCLUDED} event sequence {event.sequence}"
        )
    return IssueEvidenceRow(
        store_code=store_code,
        item_code=item_code,
        selling_uom=selling_uom,
        rule_id=rule_id,
        issue_code=issue_code,
        severity=severity,
        evidence=cast("Mapping[str, object]", evidence),
        keyless=True,
    )


def _required_text(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be text")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional value must be text")
    return value


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("evidence must be an object")
    return cast("Mapping[str, object]", dict(value))


__all__ = ["RunEvidenceRepository"]
