"""Repository for persisted promotion decision evidence.

Records what an evaluation considered and why, without choosing a winner. The
selected candidate is linked only after its membership has been validated, so
a stored SELECTED outcome always points at a candidate of the same evaluation.

No method commits a caller's transaction.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from esl_service.domain.promotion_evidence import (
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
)
from esl_service.domain.serialization import canonical_payload
from esl_service.persistence.models import (
    PromotionCandidateSnapshot,
    PromotionEvaluation,
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
