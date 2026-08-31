"""Safe retention and purge of detailed processing evidence (architecture 5.8).

Purging deletes durable evidence, so every guard here is a safety boundary:

* purge is **disabled by default** and refuses to run without explicitly
  configured durations — no retention period is ever defaulted, because they
  remain UNKNOWN / NEEDS-DISCOVERY;
* an execution is eligible only when it is terminal, its reconciliation is
  finalized with no unresolved outcome, it is older than the configured age,
  and **no action was left OUTCOME_UNKNOWN**;
* the audit core is retained and the purge records its own audit entry.

**Known limitation (issue #64).** Section 5.8 classifies canonical snapshots as
purgeable detailed evidence, but ``record_action.record_processing_result_id``
and ``record_processing_result.canonical_record_snapshot_id`` are NOT NULL with
RESTRICT. Retaining the audit core therefore pins the snapshots, so this purge
covers everything the foreign keys permit and leaves canonical snapshots,
snapshot sets, and record processing results in place. #64 relaxes those two
links.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Delete, delete, exists, select, update
from sqlalchemy.orm import Session

from esl_service.config import Settings
from esl_service.domain.actions import ActionState
from esl_service.domain.workflow import ExecutionStatus, is_terminal
from esl_service.persistence.models import (
    CanonicalRecordSnapshot,
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionStep,
    PromotionCandidateSnapshot,
    PromotionEvaluation,
    ReconciliationReport,
    RecordAction,
    RecordDifference,
    RecordIssue,
    RecordProcessingResult,
    SnapshotSet,
    WorkflowExecution,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository

#: Execution statuses that permit retention consideration.
TERMINAL_STATUSES = tuple(
    status.value for status in ExecutionStatus if is_terminal(status)
)

#: Audit action recorded whenever evidence is purged.
PURGE_ACTION = "RETENTION_PURGE"


class RetentionRefused(RuntimeError):
    """Raised when a purge is not permitted, rather than deleting anyway."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Configured retention periods. Nothing is defaulted (architecture 5.8)."""

    purge_enabled: bool
    audit_core_days: int | None
    detailed_evidence_days: int | None
    compatibility_days: int | None

    def __post_init__(self) -> None:
        if not self.purge_enabled:
            return
        for name in (
            "audit_core_days",
            "detailed_evidence_days",
            "compatibility_days",
        ):
            value: int | None = getattr(self, name)
            if value is None or value <= 0:
                raise ValueError(
                    f"{name} must be a positive number of days when purge is enabled"
                )

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetentionPolicy":
        """Build the policy from externalised configuration (FR-025)."""

        return cls(
            purge_enabled=settings.retention_purge_enabled,
            audit_core_days=settings.audit_core_days,
            detailed_evidence_days=settings.detailed_evidence_days,
            compatibility_days=settings.compatibility_days,
        )

    @property
    def detailed_evidence_age(self) -> timedelta:
        """Return how old an execution must be before its detail may be purged."""

        if self.detailed_evidence_days is None:
            raise ValueError(
                "detailed_evidence_days is not configured, so no age can be derived"
            )
        return timedelta(days=self.detailed_evidence_days)


@dataclass(frozen=True)
class PurgeOutcome:
    """How much detailed evidence one purge removed, per table."""

    execution_id: UUID
    deleted: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        """Return the total number of rows removed."""

        return sum(count for _, count in self.deleted)


class RetentionService:
    """Finds and purges executions whose detailed evidence may be removed."""

    def __init__(self, session: Session, policy: RetentionPolicy) -> None:
        self._session = session
        self._policy = policy

    def find_eligible(self, *, now: datetime, limit: int) -> list[UUID]:
        """Return executions safe to purge, oldest first.

        Every clause is a safety guard: a run that is still active, not
        reconciled, still unresolved, too recent, or holding an action with an
        unknown external outcome is never returned.
        """

        if not self._policy.purge_enabled:
            return []

        cutoff = now - self._policy.detailed_evidence_age
        statement = (
            select(WorkflowExecution.id)
            .join(
                ReconciliationReport,
                ReconciliationReport.execution_id == WorkflowExecution.id,
            )
            .where(
                WorkflowExecution.status.in_(TERMINAL_STATUSES),
                WorkflowExecution.ended_at.is_not(None),
                WorkflowExecution.ended_at < cutoff,
                ReconciliationReport.status == "FINALIZED",
                ReconciliationReport.unresolved == 0,
                ~exists().where(
                    RecordAction.execution_id == WorkflowExecution.id,
                    RecordAction.state == ActionState.OUTCOME_UNKNOWN.value,
                ),
            )
            .order_by(WorkflowExecution.ended_at)
            .limit(limit)
        )
        return list(self._session.scalars(statement))

    def purge_execution(
        self, execution_id: UUID, *, now: datetime, actor: str, reason: str
    ) -> PurgeOutcome:
        """Delete one eligible execution's detailed evidence, retaining audit core.

        Refuses a disabled policy or an ineligible execution rather than
        deleting anyway, and records its own audit entry. Commits nothing.
        """

        if not self._policy.purge_enabled:
            raise RetentionRefused("retention purge is disabled")
        if execution_id not in self.find_eligible(now=now, limit=_ELIGIBILITY_SCAN):
            raise RetentionRefused(
                f"execution {execution_id} is not eligible for purge"
            )

        deleted = self._delete_detailed_evidence(execution_id)

        ReconciliationRepository(self._session).append_audit_entry(
            actor=actor,
            action=PURGE_ACTION,
            reason=reason,
            resource_type="workflow_execution",
            resource_key=str(execution_id),
            outcome="PURGED",
            execution_id=execution_id,
            after_evidence={name: count for name, count in deleted},
        )
        self._session.flush()
        return PurgeOutcome(execution_id=execution_id, deleted=deleted)

    def _delete_detailed_evidence(
        self, execution_id: UUID
    ) -> tuple[tuple[str, int], ...]:
        """Delete detailed evidence in foreign-key-safe order."""

        results = select(RecordProcessingResult.id).where(
            RecordProcessingResult.execution_id == execution_id
        )
        snapshots = select(CanonicalRecordSnapshot.id).join(SnapshotSet).where(
            SnapshotSet.execution_id == execution_id
        )
        evaluations = select(PromotionEvaluation.id).where(
            PromotionEvaluation.canonical_record_snapshot_id.in_(snapshots)
        )
        steps = select(ExecutionStep.id).where(
            ExecutionStep.execution_id == execution_id
        )

        counts: list[tuple[str, int]] = []

        counts.append(
            self._delete(
                "record_difference",
                delete(RecordDifference).where(
                    RecordDifference.execution_id == execution_id
                ),
            )
        )
        # The selected-candidate link is cleared first: evaluation and candidate
        # reference each other, so neither can be deleted while it stands.
        self._session.execute(
            update(PromotionEvaluation)
            .where(PromotionEvaluation.id.in_(evaluations))
            .values(selected_candidate_id=None)
        )
        counts.append(
            self._delete(
                "promotion_candidate_snapshot",
                delete(PromotionCandidateSnapshot).where(
                    PromotionCandidateSnapshot.evaluation_id.in_(evaluations)
                ),
            )
        )
        counts.append(
            self._delete(
                "promotion_evaluation",
                delete(PromotionEvaluation).where(
                    PromotionEvaluation.id.in_(evaluations)
                ),
            )
        )
        counts.append(
            self._delete(
                "record_issue",
                delete(RecordIssue).where(RecordIssue.result_id.in_(results)),
            )
        )
        counts.append(
            self._delete(
                "execution_checkpoint",
                delete(ExecutionCheckpoint).where(
                    ExecutionCheckpoint.step_id.in_(steps)
                ),
            )
        )
        counts.append(
            self._delete(
                "execution_step",
                delete(ExecutionStep).where(
                    ExecutionStep.execution_id == execution_id
                ),
            )
        )
        counts.append(
            self._delete(
                "execution_event",
                delete(ExecutionEvent).where(
                    ExecutionEvent.execution_id == execution_id
                ),
            )
        )
        self._session.flush()
        return tuple(counts)

    def _delete(self, table: str, statement: Delete) -> tuple[str, int]:
        """Run one delete and report how many rows it removed."""

        result = cast("CursorResult[Any]", self._session.execute(statement))
        return table, int(result.rowcount or 0)


#: Upper bound when re-checking eligibility for a specific execution.
_ELIGIBILITY_SCAN = 1000
