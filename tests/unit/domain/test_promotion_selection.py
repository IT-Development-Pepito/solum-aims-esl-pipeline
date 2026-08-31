"""Conservative promotion compatibility selection (BR-019, BR-020).

Promotion priority is **not formally defined** (reference section 7.1), so this
strategy never invents one. It selects only where selecting is not a business
decision, and otherwise records the ambiguity and leaves the outcome
unresolved.

Deployed compatibility is not claimed here; #38 and representative cases
remain required before any such claim.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.promotion_evidence import (
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)
from esl_service.domain.promotion_rules import (
    CampaignCandidate,
    build_state,
    evaluate,
    evaluate_candidate,
)
from esl_service.domain.promotion_selection import (
    REASON_DISPLAY_PRIORITY_SAME_ECONOMIC,
    REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC,
    SELECTION_STRATEGY_VERSION,
    select_compatibility_state,
)

KEY = CanonicalKey("084", "101024011793", "KGS")
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
REGULAR = Decimal(74500)


def campaign(**overrides: object) -> CampaignCandidate:
    """Build one eligible campaign."""

    values: dict[str, object] = {
        "campaign_id": "A",
        "campaign_group": "REGULAR",
        "promotion_type": PromotionType.PERCENT,
        "structured_value": Decimal(50),
        "raw_disc_text": "DISC 50%|LIMIT 2",
        "start_date": date(2026, 8, 28),
        "end_date": date(2026, 8, 30),
        "start_time": time(7, 0),
        "end_time": time(23, 0),
        "campaign_uom": "KGS",
        "weekday": WeekdayEvidence.ACTIVE,
    }
    values.update(overrides)
    return CampaignCandidate(**values)  # type: ignore[arg-type]


def evaluation_of(*campaigns: CampaignCandidate):
    """Evaluate campaigns for the default canonical key."""

    evidences = tuple(
        evaluate_candidate(
            key=KEY,
            candidate=item,
            now=NOW,
            regular_price=REGULAR,
            regular_price_ambiguous=False,
        )
        for item in campaigns
    )
    return evaluate(KEY, "rules-v1", "calc-v1", evidences)


# --- no invented priority (BR-019, reference 7.1) ---------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # lowest price would win
        (Decimal(50), Decimal(60)),
        # newest campaign would win
        (Decimal(40), Decimal(50)),
    ],
)
def test_different_economic_outcomes_are_never_resolved(
    first: Decimal, second: Decimal
) -> None:
    """No lowest-price, newest-campaign, or similar rule is applied."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", structured_value=first),
        campaign(campaign_id="B", structured_value=second),
    )
    selected = select_compatibility_state(evaluation)

    assert selected.outcome is PromotionOutcome.UNRESOLVED
    assert selected.selected_candidate_id is None
    assert selected.resulting_state is None


def test_a_fixed_price_never_beats_a_percent_promotion() -> None:
    """Promotion type confers no priority (reference 7.1)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", promotion_type=PromotionType.PERCENT),
        campaign(
            campaign_id="B",
            promotion_type=PromotionType.FIXED_PRICE,
            structured_value=Decimal(57900),
        ),
    )
    assert select_compatibility_state(evaluation).outcome is (
        PromotionOutcome.UNRESOLVED
    )


def test_different_economic_records_its_reason_code() -> None:
    """The condition is observable for review (reference 7.3)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", structured_value=Decimal(50)),
        campaign(
            campaign_id="B",
            promotion_type=PromotionType.FIXED_PRICE,
            structured_value=Decimal(57900),
        ),
    )
    selected = select_compatibility_state(evaluation)
    codes = {code for item in selected.candidates for code in item.reason_codes}

    assert REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC in codes


# --- same economic, different terms (reference 8) ---------------------------


def test_same_economic_with_different_terms_is_unresolved() -> None:
    """Identical price with different LIMIT wording is not equivalent."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", raw_disc_text="DISC 50%|LIMIT 2"),
        campaign(campaign_id="B", raw_disc_text="DISC 50%|LIMIT 12"),
    )
    selected = select_compatibility_state(evaluation)
    codes = {code for item in selected.candidates for code in item.reason_codes}

    assert selected.outcome is PromotionOutcome.UNRESOLVED
    assert REASON_DISPLAY_PRIORITY_SAME_ECONOMIC in codes
    assert REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC not in codes


def test_same_economic_with_a_different_campaign_group_is_unresolved() -> None:
    """Campaign group may be a real term, not decoration (reference 8)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", campaign_group="REGULAR"),
        campaign(campaign_id="B", campaign_group="CLEARANCE"),
    )
    assert select_compatibility_state(evaluation).outcome is (
        PromotionOutcome.UNRESOLVED
    )


def test_same_economic_with_a_different_period_is_unresolved() -> None:
    """A different promotion period is a different term (reference 8)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", end_date=date(2026, 8, 30)),
        campaign(campaign_id="B", end_date=date(2026, 8, 29)),
    )
    assert select_compatibility_state(evaluation).outcome is (
        PromotionOutcome.UNRESOLVED
    )


# --- safe selections --------------------------------------------------------


def test_a_single_eligible_candidate_is_selected() -> None:
    """One eligible campaign needs no priority decision."""

    selected = select_compatibility_state(evaluation_of(campaign()))

    assert selected.outcome is PromotionOutcome.SELECTED
    assert selected.selected_candidate_id == "A"
    assert selected.resulting_state is not None


def test_identical_outbound_states_are_selected_deterministically() -> None:
    """Choosing between identical states changes nothing, so it is safe."""

    evaluation = evaluation_of(
        campaign(campaign_id="B"), campaign(campaign_id="A")
    )
    first = select_compatibility_state(evaluation)
    second = select_compatibility_state(evaluation)

    assert first.outcome is PromotionOutcome.SELECTED
    assert first.selected_candidate_id == second.selected_candidate_id
    assert first.resulting_state == second.resulting_state


def test_identical_outbound_state_still_records_the_ambiguity() -> None:
    """Ambiguity stays observable even when a candidate is chosen (7.3)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A"), campaign(campaign_id="B")
    )
    selected = select_compatibility_state(evaluation)
    codes = {code for item in selected.candidates for code in item.reason_codes}

    assert selected.outcome is PromotionOutcome.SELECTED
    assert REASON_DISPLAY_PRIORITY_SAME_ECONOMIC in codes


def test_existing_state_is_retained_when_it_already_matches() -> None:
    """Retaining an unchanged display makes no new business decision."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", raw_disc_text="DISC 50%|LIMIT 2"),
        campaign(campaign_id="B", raw_disc_text="DISC 50%|LIMIT 12"),
    )
    existing = build_state(
        next(item for item in evaluation.candidates if item.candidate_id == "B")
    )
    selected = select_compatibility_state(evaluation, existing_state=existing)

    assert selected.outcome is PromotionOutcome.SELECTED
    assert selected.selected_candidate_id == "B"
    assert selected.resulting_state == existing


def test_a_non_matching_existing_state_never_forces_a_selection() -> None:
    """An existing state that no eligible candidate reproduces is not retained."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", structured_value=Decimal(50)),
        campaign(campaign_id="B", structured_value=Decimal(40)),
    )
    stale = build_state(evaluation_of(campaign(structured_value=Decimal(10))).candidates[0])
    selected = select_compatibility_state(evaluation, existing_state=stale)

    assert selected.outcome is PromotionOutcome.UNRESOLVED
    assert selected.resulting_state is None


# --- untouched outcomes and provenance --------------------------------------


def test_no_promotion_is_passed_through_unchanged() -> None:
    """An evaluation with nothing eligible is returned as it was."""

    evaluation = evaluate(KEY, "rules-v1", "calc-v1", ())
    assert select_compatibility_state(evaluation) == evaluation


def test_an_unresolved_evaluation_is_not_rescued() -> None:
    """An unconvertible UOM stays unresolved; selection cannot mask it."""

    evaluation = evaluation_of(campaign(campaign_uom="CTN"))
    selected = select_compatibility_state(evaluation)

    assert selected.outcome is PromotionOutcome.UNRESOLVED
    assert selected.resulting_state is None


def test_weekday_fallback_is_preserved_separately_from_inactive() -> None:
    """Fallback evidence survives selection and stays distinct (BR-017)."""

    evaluation = evaluation_of(campaign(weekday=WeekdayEvidence.MISSING))
    selected = select_compatibility_state(evaluation)

    assert selected.outcome is PromotionOutcome.SELECTED
    assert "MISSING_WEEKDAY_METADATA" in selected.candidates[0].fallback_codes
    assert "WEEKDAY_INACTIVE" not in selected.candidates[0].reason_codes


def test_every_candidate_and_its_calculation_survive_selection() -> None:
    """The audit can still answer alternatives, price, UOM, and calculation."""

    evaluation = evaluation_of(
        campaign(campaign_id="A", structured_value=Decimal(50)),
        campaign(campaign_id="B", structured_value=Decimal(40)),
    )
    selected = select_compatibility_state(evaluation)

    assert {item.source_campaign_id for item in selected.candidates} == {"A", "B"}
    for item in selected.candidates:
        assert item.category_001_regular_price == REGULAR
        assert item.resolved_selling_uom == "KGS"
        assert item.calculated_effective_price is not None


def test_the_strategy_is_versioned() -> None:
    """A future approved priority rule replaces a named, versioned strategy."""

    assert SELECTION_STRATEGY_VERSION.startswith("compatibility-")


def test_module_exposes_no_business_priority_rule() -> None:
    """No lowest-price or similar helper may exist in this module."""

    import esl_service.domain.promotion_selection as module

    assert not [
        name
        for name in dir(module)
        if any(term in name.lower() for term in ("lowest", "cheapest", "newest", "wins"))
    ]
