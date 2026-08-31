"""Integration coverage for persisted promotion evidence (BR-016, BR-019).

Every candidate considered is retained so an ambiguous or unresolved decision
stays auditable. No winner is chosen here; #37 consumes this evidence.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.promotion_evidence import (
    CandidateEligibility,
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)
from esl_service.domain.promotion_rules import build_state
from esl_service.persistence.evidence_repository import PromotionEvidenceRepository
from esl_service.persistence.models import PromotionEvaluation
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")
STARTS = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
ENDS = datetime(2026, 8, 30, 23, 0, tzinfo=UTC)


@pytest.fixture
def snapshot_id(
    session: Session,
    execution_repository: ExecutionRepository,
    snapshot_repository: SnapshotRepository,
    configuration_version_id: UUID,
) -> UUID:
    """Persist one canonical snapshot the evaluation attaches to."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark="2026-08-28T07:00:00+00:00",
        canonical_schema_version="canonical-v1",
    )
    record = snapshot_repository.append_record(snapshot_set.id, canonical_record())
    session.flush()
    return record.id


def candidate(**overrides: object) -> PromotionCandidateEvidence:
    """Build one candidate evidence record."""

    values: dict[str, object] = {
        "candidate_id": "A",
        "key": KEY,
        "source_campaign_id": "A",
        "campaign_group": "REGULAR",
        "promotion_type": PromotionType.PERCENT,
        "structured_value": Decimal(50),
        "raw_disc_text": "DISC 50%|ALL ITEM",
        "starts_at": STARTS,
        "ends_at": ENDS,
        "weekday_evidence": WeekdayEvidence.ACTIVE,
        "category_001_regular_price": Decimal("74500.0000"),
        "source_uom": "KGS",
        "resolved_selling_uom": "KGS",
        "calculated_effective_price": Decimal("37250.0000"),
        "display_price": Decimal("3725.0000"),
        "eligibility": CandidateEligibility.ELIGIBLE,
        "reason_codes": (),
        "fallback_codes": (),
    }
    values.update(overrides)
    return PromotionCandidateEvidence(**values)  # type: ignore[arg-type]


def evaluation(**overrides: object) -> PromotionEvaluationEvidence:
    """Build an evaluation, ambiguous by default so no winner is implied."""

    values: dict[str, object] = {
        "key": KEY,
        "rule_version": "rules-v1",
        "calculation_version": "calc-v1",
        "outcome": PromotionOutcome.AMBIGUOUS,
        "candidates": (
            candidate(candidate_id="A", source_campaign_id="A"),
            candidate(candidate_id="B", source_campaign_id="B"),
        ),
        "selected_candidate_id": None,
        "resulting_state": None,
    }
    values.update(overrides)
    return PromotionEvaluationEvidence(**values)  # type: ignore[arg-type]


def test_ambiguous_evaluation_retains_all_candidates(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """Ambiguity is observable and no candidate is discarded (BR-019)."""

    stored = promotion_repository.record_evaluation(snapshot_id, evaluation())
    session.flush()

    assert stored.outcome == "AMBIGUOUS"
    assert {row.source_campaign_id for row in stored.candidates} == {"A", "B"}
    assert stored.selected_candidate_id is None
    assert stored.resulting_state is None


def test_selected_evaluation_stores_its_atomic_state(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """A selected outcome persists one candidate's complete state (BR-016)."""

    only = candidate()
    promotion_repository.record_evaluation(
        snapshot_id,
        evaluation(
            outcome=PromotionOutcome.SELECTED,
            candidates=(only,),
            selected_candidate_id="A",
            resulting_state=build_state(only),
        ),
    )
    session.flush()
    session.expire_all()

    reloaded = promotion_repository.get_evaluation(snapshot_id, "rules-v1", "calc-v1")
    assert reloaded is not None
    assert reloaded.outcome == "SELECTED"
    assert reloaded.selected_candidate_id is not None
    assert reloaded.resulting_state is not None
    assert reloaded.resulting_state["source_campaign_id"] == "A"
    selected = next(
        row for row in reloaded.candidates if row.id == reloaded.selected_candidate_id
    )
    assert selected.source_campaign_id == "A"


def test_evaluation_is_unique_per_snapshot_rule_and_calculation(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """One evaluation exists per snapshot and rule/calculation version."""

    promotion_repository.record_evaluation(snapshot_id, evaluation())
    session.flush()

    with pytest.raises(IntegrityError):
        promotion_repository.record_evaluation(snapshot_id, evaluation())
        session.flush()


def test_candidate_is_unique_per_evaluation_and_campaign(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """A campaign appears once within one evaluation."""

    stored = promotion_repository.record_evaluation(snapshot_id, evaluation())
    session.flush()

    duplicate = type(stored.candidates[0])(
        evaluation_id=stored.id,
        source_campaign_id="A",
        campaign_group=None,
        promotion_type="PERCENT",
        structured_value=Decimal(10),
        raw_disc_text=None,
        starts_at=STARTS,
        ends_at=ENDS,
        weekday_evidence="ACTIVE",
        category_001_regular_price=None,
        source_uom="KGS",
        resolved_selling_uom="KGS",
        calculated_effective_price=None,
        display_price=None,
        eligibility="ELIGIBLE",
        reason_codes=[],
        fallback_codes=[],
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()


def test_decimal_and_raw_text_survive_the_round_trip(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """NUMERIC precision and manual text are preserved exactly (BR-011)."""

    text = "BELI 2 GRATIS 1 | PROMO   AKHIR PEKAN "
    stored = promotion_repository.record_evaluation(
        snapshot_id,
        evaluation(
            candidates=(
                candidate(
                    raw_disc_text=text,
                    structured_value=Decimal("12.3400"),
                    category_001_regular_price=Decimal("74500.5000"),
                ),
                candidate(candidate_id="B", source_campaign_id="B"),
            )
        ),
    )
    session.flush()
    session.expire_all()

    row = next(item for item in stored.candidates if item.source_campaign_id == "A")
    assert row.raw_disc_text == text
    assert row.structured_value == Decimal("12.3400")
    assert row.category_001_regular_price == Decimal("74500.5000")


def test_missing_and_inactive_weekday_are_distinct(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """Absent metadata is stored distinctly from explicitly inactive (BR-017)."""

    stored = promotion_repository.record_evaluation(
        snapshot_id,
        evaluation(
            outcome=PromotionOutcome.NO_PROMOTION,
            candidates=(
                candidate(
                    candidate_id="A",
                    source_campaign_id="A",
                    weekday_evidence=WeekdayEvidence.MISSING,
                    eligibility=CandidateEligibility.ELIGIBLE,
                    fallback_codes=("MISSING_WEEKDAY_METADATA",),
                ),
                candidate(
                    candidate_id="B",
                    source_campaign_id="B",
                    weekday_evidence=WeekdayEvidence.INACTIVE,
                    eligibility=CandidateEligibility.INELIGIBLE,
                    reason_codes=("WEEKDAY_INACTIVE",),
                ),
            ),
        ),
    )
    session.flush()

    by_campaign = {row.source_campaign_id: row for row in stored.candidates}
    assert by_campaign["A"].weekday_evidence == "MISSING"
    assert by_campaign["A"].fallback_codes == ["MISSING_WEEKDAY_METADATA"]
    assert by_campaign["B"].weekday_evidence == "INACTIVE"
    assert by_campaign["B"].reason_codes == ["WEEKDAY_INACTIVE"]


def test_unresolved_uom_evidence_is_retained(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """An unconvertible campaign UOM is kept for escalation, not dropped."""

    stored = promotion_repository.record_evaluation(
        snapshot_id,
        evaluation(
            outcome=PromotionOutcome.UNRESOLVED,
            candidates=(
                candidate(
                    source_uom="CTN",
                    resolved_selling_uom=None,
                    calculated_effective_price=None,
                    display_price=None,
                    eligibility=CandidateEligibility.UNRESOLVED,
                    reason_codes=("UOM_RULE_REQUIRED",),
                ),
            ),
        ),
    )
    session.flush()

    assert stored.outcome == "UNRESOLVED"
    assert stored.candidates[0].resolved_selling_uom is None
    assert stored.candidates[0].reason_codes == ["UOM_RULE_REQUIRED"]


def test_evaluation_with_candidates_cannot_be_deleted(
    session: Session,
    promotion_repository: PromotionEvidenceRepository,
    snapshot_id: UUID,
) -> None:
    """Durable evidence uses RESTRICT so an audit trail survives."""

    stored = promotion_repository.record_evaluation(snapshot_id, evaluation())
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            delete(PromotionEvaluation).where(PromotionEvaluation.id == stored.id)
        )
        session.flush()


def test_get_evaluation_returns_none_when_absent(
    promotion_repository: PromotionEvidenceRepository,
) -> None:
    """A missing evaluation is an explicit absence, not an error."""

    assert promotion_repository.get_evaluation(uuid4(), "rules-v1", "calc-v1") is None
