"""Repository for persisted promotion decision evidence.

Records what an evaluation considered and why, without choosing a winner. The
selected candidate is linked only after its membership has been validated, so
a stored SELECTED outcome always points at a candidate of the same evaluation.

No method commits a caller's transaction.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from esl_service.domain.outcomes import RecordProcessingEvidence
from esl_service.domain.promotion_evidence import (
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
)
from esl_service.domain.serialization import canonical_payload, sanitize_evidence
from esl_service.persistence.models import (
    PromotionCandidateSnapshot,
    PromotionEvaluation,
    RecordIssue,
    RecordProcessingResult,
)


class PromotionEvidenceRepository:
    """Persists promotion evaluations and every candidate they considered."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_evaluation(
        self, snapshot_id: UUID, evidence: PromotionEvaluationEvidence
    ) -> PromotionEvaluation:
        """Persist one evaluation with all of its candidate evidence."""

        state = evidence.resulting_state
        evaluation = PromotionEvaluation(
            canonical_record_snapshot_id=snapshot_id,
            rule_version=evidence.rule_version,
            calculation_version=evidence.calculation_version,
            outcome=evidence.outcome.value,
            resulting_state=None if state is None else canonical_payload(state),
        )
        self._session.add(evaluation)
        self._session.flush()

        stored_by_candidate_id: dict[str, PromotionCandidateSnapshot] = {}
        for candidate in evidence.candidates:
            stored = self._build_candidate(evaluation.id, candidate)
            self._session.add(stored)
            stored_by_candidate_id[candidate.candidate_id] = stored
        self._session.flush()

        if evidence.outcome is PromotionOutcome.SELECTED:
            selected = stored_by_candidate_id.get(evidence.selected_candidate_id or "")
            if selected is None:
                raise ValueError(
                    "selected candidate must be one of the recorded candidates"
                )
            evaluation.selected_candidate_id = selected.id
            self._session.flush()

        return evaluation

    def get_evaluation(
        self, snapshot_id: UUID, rule_version: str, calculation_version: str
    ) -> PromotionEvaluation | None:
        """Return one evaluation and its candidates, or None when absent."""

        statement = (
            select(PromotionEvaluation)
            .where(
                PromotionEvaluation.canonical_record_snapshot_id == snapshot_id,
                PromotionEvaluation.rule_version == rule_version,
                PromotionEvaluation.calculation_version == calculation_version,
            )
            .options(selectinload(PromotionEvaluation.candidates))
        )
        return self._session.scalars(statement).one_or_none()

    @staticmethod
    def _build_candidate(
        evaluation_id: UUID, candidate: PromotionCandidateEvidence
    ) -> PromotionCandidateSnapshot:
        """Map one candidate's evidence onto its persistent row."""

        return PromotionCandidateSnapshot(
            evaluation_id=evaluation_id,
            source_campaign_id=candidate.source_campaign_id,
            campaign_group=candidate.campaign_group,
            promotion_type=candidate.promotion_type.value,
            structured_value=candidate.structured_value,
            raw_disc_text=candidate.raw_disc_text,
            starts_at=candidate.starts_at,
            ends_at=candidate.ends_at,
            weekday_evidence=candidate.weekday_evidence.value,
            category_001_regular_price=candidate.category_001_regular_price,
            source_uom=candidate.source_uom,
            resolved_selling_uom=candidate.resolved_selling_uom,
            calculated_effective_price=candidate.calculated_effective_price,
            display_price=candidate.display_price,
            eligibility=candidate.eligibility.value,
            reason_codes=list(candidate.reason_codes),
            fallback_codes=list(candidate.fallback_codes),
        )


class RecordOutcomeRepository:
    """Persists per-record processing outcomes and their issues.

    Every issue is stored as its own row, so a record that failed for several
    independent reasons keeps all of them queryable (FR-006, FR-022).
    """

    #: Schema version of the sanitized issue evidence payload.
    EVIDENCE_SCHEMA_VERSION = "record-issue-v1"

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_processing_result(
        self,
        execution_id: UUID,
        snapshot_id: UUID,
        evidence: RecordProcessingEvidence,
    ) -> RecordProcessingResult:
        """Persist one record's outcome together with all of its issues."""

        result = RecordProcessingResult(
            execution_id=execution_id,
            canonical_record_snapshot_id=snapshot_id,
            store_code=evidence.key.store_code,
            item_code=evidence.key.item_code,
            selling_uom=evidence.key.selling_uom,
            validation_status=evidence.validation_status.value,
            eligibility_status=evidence.eligibility_status.value,
            promotion_outcome=(
                None
                if evidence.promotion_outcome is None
                else evidence.promotion_outcome.value
            ),
            current_page=evidence.current_page,
            desired_page=evidence.desired_page,
            action_decision=evidence.action_decision.value,
            processing_status=evidence.processing_status.value,
        )
        self._session.add(result)
        self._session.flush()

        for position, item in enumerate(evidence.issues):
            self._session.add(
                RecordIssue(
                    result_id=result.id,
                    sequence=position,
                    rule_id=item.rule_id,
                    issue_code=item.issue_code,
                    severity=item.severity,
                    classification=item.classification,
                    evidence_schema_version=self.EVIDENCE_SCHEMA_VERSION,
                    # Re-checked here so persistence cannot store a secret even
                    # if a caller built the contract by another route.
                    evidence=sanitize_evidence(dict(item.evidence)),
                )
            )
        self._session.flush()
        return result

    def list_results(self, execution_id: UUID) -> list[RecordProcessingResult]:
        """Return one execution's record outcomes with their issues."""

        statement = (
            select(RecordProcessingResult)
            .where(RecordProcessingResult.execution_id == execution_id)
            .order_by(
                RecordProcessingResult.store_code,
                RecordProcessingResult.item_code,
                RecordProcessingResult.selling_uom,
            )
            .options(selectinload(RecordProcessingResult.issues))
        )
        return list(self._session.scalars(statement))
