"""Invariants of the promotion evidence contracts (BR-016, BR-018, BR-019).

These contracts retain what was considered and why. They deliberately contain
no winner-selection policy: campaign priority is UNKNOWN / NEEDS-DISCOVERY and
is consumed later by #37.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from esl_service.domain.canonical import CanonicalKey, PromotionStateData
from esl_service.domain.promotion_evidence import (
    CandidateEligibility,
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)

KEY = CanonicalKey("084", "101024011793", "KGS")
STARTS = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
ENDS = datetime(2026, 8, 30, 23, 59, tzinfo=UTC)


def candidate(**overrides: object) -> PromotionCandidateEvidence:
    """Build one candidate evidence record."""

    values: dict[str, object] = {
        "candidate_id": "A",
        "key": KEY,
        "source_campaign_id": "A",
        "campaign_group": "REGULAR",
        "promotion_type": PromotionType.PERCENT,
        "structured_value": Decimal(50),
        "raw_disc_text": "DISC 50%",
        "starts_at": STARTS,
        "ends_at": ENDS,
        "weekday_evidence": WeekdayEvidence.ACTIVE,
        "category_001_regular_price": Decimal(74500),
        "source_uom": "KGS",
        "resolved_selling_uom": "KGS",
        "calculated_effective_price": Decimal(37250),
        "display_price": Decimal(3725),
        "eligibility": CandidateEligibility.ELIGIBLE,
        "reason_codes": (),
        "fallback_codes": (),
    }
    values.update(overrides)
    return PromotionCandidateEvidence(**values)  # type: ignore[arg-type]


def state_from(evidence: PromotionCandidateEvidence) -> PromotionStateData:
    """Build the atomic state a SELECTED outcome must carry."""

    return PromotionStateData(
        source_campaign_id=evidence.source_campaign_id,
        promotion_flag=None,
        promotion_type=evidence.promotion_type.value,
        campaign_group=evidence.campaign_group,
        structured_value=evidence.structured_value,
        effective_price=evidence.calculated_effective_price,
        display_price=evidence.display_price,
        discount_percentage=evidence.structured_value,
        saving_amount=None,
        raw_disc_text=evidence.raw_disc_text,
        starts_at=evidence.starts_at,
        ends_at=evidence.ends_at,
    )


def test_selected_outcome_requires_a_member_candidate() -> None:
    """A selected state must come from a candidate that was actually considered."""

    only = candidate()
    with pytest.raises(ValueError, match="selected candidate"):
        PromotionEvaluationEvidence(
            key=KEY,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=PromotionOutcome.SELECTED,
            candidates=(only,),
            selected_candidate_id="different",
            resulting_state=state_from(only),
        )


def test_selected_state_must_match_its_candidate() -> None:
    """A mixed state drawn from more than one campaign is rejected (BR-016)."""

    first = candidate(candidate_id="A", source_campaign_id="A")
    mixed = state_from(candidate(candidate_id="B", source_campaign_id="B"))
    with pytest.raises(ValueError, match="atomic"):
        PromotionEvaluationEvidence(
            key=KEY,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=PromotionOutcome.SELECTED,
            candidates=(first,),
            selected_candidate_id="A",
            resulting_state=mixed,
        )


@pytest.mark.parametrize(
    "outcome",
    [
        PromotionOutcome.NO_PROMOTION,
        PromotionOutcome.AMBIGUOUS,
        PromotionOutcome.REJECTED,
        PromotionOutcome.UNRESOLVED,
    ],
)
def test_only_selected_may_carry_a_state(outcome: PromotionOutcome) -> None:
    """An unresolved or ambiguous evaluation never yields a promotion state."""

    only = candidate()
    with pytest.raises(ValueError, match="only a SELECTED"):
        PromotionEvaluationEvidence(
            key=KEY,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=outcome,
            candidates=(only,),
            selected_candidate_id=None,
            resulting_state=state_from(only),
        )


def test_ambiguous_evaluation_retains_every_candidate() -> None:
    """Ambiguity is observable: all candidates survive for review (BR-019)."""

    evaluation = PromotionEvaluationEvidence(
        key=KEY,
        rule_version="rules-v1",
        calculation_version="calc-v1",
        outcome=PromotionOutcome.AMBIGUOUS,
        candidates=(candidate(candidate_id="A"), candidate(candidate_id="B")),
        selected_candidate_id=None,
        resulting_state=None,
    )
    assert len(evaluation.candidates) == 2
    assert evaluation.selected_candidate_id is None


def test_candidate_identifiers_must_be_unique() -> None:
    """Two candidates cannot share an identifier within one evaluation."""

    with pytest.raises(ValueError, match="unique"):
        PromotionEvaluationEvidence(
            key=KEY,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=PromotionOutcome.AMBIGUOUS,
            candidates=(candidate(candidate_id="A"), candidate(candidate_id="A")),
            selected_candidate_id=None,
            resulting_state=None,
        )


def test_candidate_may_not_cross_the_evaluation_key() -> None:
    """No candidate crosses a store or selling-UOM boundary (BR-018)."""

    with pytest.raises(ValueError, match="canonical key"):
        PromotionEvaluationEvidence(
            key=KEY,
            rule_version="rules-v1",
            calculation_version="calc-v1",
            outcome=PromotionOutcome.AMBIGUOUS,
            candidates=(
                candidate(candidate_id="A"),
                candidate(candidate_id="B", key=CanonicalKey("075", "1", "KGS")),
            ),
            selected_candidate_id=None,
            resulting_state=None,
        )


def test_module_exposes_no_winner_selection() -> None:
    """Campaign priority is unresolved, so no selector may exist here."""

    import esl_service.domain.promotion_evidence as module

    assert not [name for name in dir(module) if "select" in name.lower()]
