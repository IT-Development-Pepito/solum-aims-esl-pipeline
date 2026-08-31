"""Repository for the audit ledger and reconciliation reports (FR-021, FR-022).

``finalize_report`` refuses to persist an imbalance: the balance rules are
validated by the domain before anything is written, so a stored report is
always a balanced accounting. Exceptions are enumerated from the durable
evidence already recorded by earlier stages — record issues from #12 and
actions with an unknown outcome from #19 — rather than being recomputed.

No method commits a caller's transaction.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from esl_service.domain.actions import ActionState
from esl_service.domain.reconciliation import (
    ReconciliationCounts,
    ReconciliationMode,
    validate_balance,
)
from esl_service.domain.serialization import JSONValue, sanitize_evidence
from esl_service.persistence.models import (
    AuditEntry,
    ExecutionEvent,
    ReconciliationException,
    ReconciliationReport,
    RecordAction,
    RecordIssue,
    RecordProcessingResult,
)

#: Schema version of the sanitized audit and exception evidence payloads.
EVIDENCE_SCHEMA_VERSION = "audit-evidence-v1"

#: Category used when an action's external outcome could not be determined.
CATEGORY_ACTION_OUTCOME_UNKNOWN = "ACTION_OUTCOME_UNKNOWN"

RESOLUTION_OPEN = "OPEN"


class ReconciliationRepository:
    """Persists balanced reconciliation reports and the durable audit ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def finalize_report(
        self,
        execution_id: UUID,
        mode: ReconciliationMode,
        counts: ReconciliationCounts,
    ) -> ReconciliationReport:
        """Validate the balance, then persist an immutable report revision.

        A finalized report is never overwritten: re-reconciling an execution
        creates the next revision so earlier evidence survives (architecture
        5.7).
        """

        validate_balance(mode, counts)

        next_revision = (
            self._session.scalars(
                select(func.coalesce(func.max(ReconciliationReport.revision), 0))
                .where(ReconciliationReport.execution_id == execution_id)
            ).one()
            + 1
        )

        report = ReconciliationReport(
            execution_id=execution_id,
            revision=next_revision,
            mode=mode.value,
            status="FINALIZED",
            extracted=counts.extracted,
            rejected=counts.rejected,
            valid=counts.valid,
            ineligible=counts.ineligible,
            eligible=counts.eligible,
            unchanged=counts.unchanged,
            skipped_idempotent=counts.skipped_idempotent,
            intended=counts.intended,
            acknowledged=counts.acknowledged,
            rejected_by_aims=counts.rejected_by_aims,
            failed=counts.failed,
            unresolved=counts.unresolved,
            submitted=counts.submitted,
            ambiguous=counts.ambiguous,
            finalized_at=datetime.now(UTC),
        )
        self._session.add(report)
        self._session.flush()

        self._enumerate_exceptions(report, execution_id)
        self._session.flush()
        return report

    def list_exceptions(self, report_id: UUID) -> list[ReconciliationException]:
        """Return one report's enumerated exceptions in stable order."""

        statement = (
            select(ReconciliationException)
            .where(ReconciliationException.report_id == report_id)
            .order_by(ReconciliationException.sequence)
        )
        return list(self._session.scalars(statement))

    def append_audit_entry(
        self,
        *,
        actor: str,
        action: str,
        reason: str,
        resource_type: str,
        resource_key: str,
        outcome: str,
        execution_id: UUID | None = None,
        configuration_version_id: UUID | None = None,
        correlation_id: UUID | None = None,
        before_evidence: Mapping[str, JSONValue] | None = None,
        after_evidence: Mapping[str, JSONValue] | None = None,
    ) -> AuditEntry:
        """Append one immutable audit record (FR-008, FR-011, FR-022, FR-023)."""

        entry = AuditEntry(
            execution_id=execution_id,
            configuration_version_id=configuration_version_id,
            correlation_id=correlation_id,
            actor=actor,
            action=action,
            reason=reason,
            resource_type=resource_type,
            resource_key=resource_key,
            outcome=outcome,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            before_evidence=_sanitized(before_evidence),
            after_evidence=_sanitized(after_evidence),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def query_audit(self, execution_id: UUID) -> list[AuditEntry]:
        """Return one execution's audit trail in occurrence order."""

        statement = (
            select(AuditEntry)
            .where(AuditEntry.execution_id == execution_id)
            .order_by(AuditEntry.sequence)
        )
        return list(self._session.scalars(statement))

    def query_events(self, execution_id: UUID) -> list[ExecutionEvent]:
        """Return one execution's structured events in order (NFR-007)."""

        statement = (
            select(ExecutionEvent)
            .where(ExecutionEvent.execution_id == execution_id)
            .order_by(ExecutionEvent.sequence)
        )
        return list(self._session.scalars(statement))

    def _enumerate_exceptions(
        self, report: ReconciliationReport, execution_id: UUID
    ) -> None:
        """Enumerate every anomaly from durable evidence, not from aggregates.

        Record issues are surfaced by their issue code, so a category such as
        ``UOM_RULE_REQUIRED`` or ``MISSING_WEEKDAY_METADATA`` reaches the report
        exactly as the rules recorded it.
        """

        sequence = 0

        issues = self._session.scalars(
            select(RecordIssue)
            .join(RecordProcessingResult)
            .where(RecordProcessingResult.execution_id == execution_id)
            .order_by(
                RecordProcessingResult.store_code,
                RecordProcessingResult.item_code,
                RecordProcessingResult.selling_uom,
                RecordIssue.sequence,
            )
            .options(selectinload(RecordIssue.result))
        ).all()
        for issue in issues:
            self._session.add(
                ReconciliationException(
                    report_id=report.id,
                    sequence=sequence,
                    category=issue.issue_code,
                    record_processing_result_id=issue.result_id,
                    store_code=issue.result.store_code,
                    item_code=issue.result.item_code,
                    selling_uom=issue.result.selling_uom,
                    actual_evidence=dict(issue.evidence),
                    resolution_status=RESOLUTION_OPEN,
                )
            )
            sequence += 1

        unknown_actions = self._session.scalars(
            select(RecordAction)
            .where(
                RecordAction.execution_id == execution_id,
                RecordAction.state == ActionState.OUTCOME_UNKNOWN.value,
            )
            .order_by(RecordAction.occurred_at, RecordAction.id)
        ).all()
        for unknown in unknown_actions:
            self._session.add(
                ReconciliationException(
                    report_id=report.id,
                    sequence=sequence,
                    category=CATEGORY_ACTION_OUTCOME_UNKNOWN,
                    record_processing_result_id=unknown.record_processing_result_id,
                    record_action_id=unknown.id,
                    store_code=unknown.store_code,
                    item_code=unknown.item_code,
                    selling_uom=unknown.selling_uom,
                    expected_evidence={"desired_state": unknown.desired_state},
                    actual_evidence={
                        "state": unknown.state,
                        "idempotency_key": unknown.idempotency_key,
                    },
                    resolution_status=RESOLUTION_OPEN,
                )
            )
            sequence += 1


def _sanitized(
    evidence: Mapping[str, JSONValue] | None,
) -> dict[str, JSONValue] | None:
    """Reject secret-like keys before evidence reaches the audit ledger."""

    if evidence is None:
        return None
    sanitized = sanitize_evidence(dict(evidence))
    assert isinstance(sanitized, dict)
    return sanitized
