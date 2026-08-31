"""Reference-directed promotion rules (BR-011 through BR-018).

Every rule here is traceable to
``docs/sql-server/ESL_Promotion_Business_Logic_and_Business_Rules_Reference.md``.
No rule selects a winner among several eligible campaigns: that remains
UNKNOWN / NEEDS-DISCOVERY and belongs to #37. Deployed legacy parity is not
claimed here; #38 is that gate.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.promotion_evidence import (
    CandidateEligibility,
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)
from esl_service.domain.promotion_rules import (
    CampaignCandidate,
    calculate_effective_price,
    evaluate,
    evaluate_candidate,
    is_pfs_excluded,
    is_within_window,
    resolve_selling_uom,
    to_display_price,
)

KEY = CanonicalKey("084", "101024011793", "KGS")
REGULAR = Decimal(74500)


def campaign(**overrides: object) -> CampaignCandidate:
    """Build a campaign candidate, overriding only what a test needs."""

    values: dict[str, object] = {
        "campaign_id": "A",
        "campaign_group": "REGULAR",
        "promotion_type": PromotionType.PERCENT,
        "structured_value": Decimal(50),
        "raw_disc_text": "DISC 50%|ALL ITEM",
        "start_date": date(2026, 8, 28),
        "end_date": date(2026, 8, 30),
        "start_time": time(7, 0),
        "end_time": time(23, 0),
        "campaign_uom": "KGS",
        "weekday": WeekdayEvidence.ACTIVE,
    }
    values.update(overrides)
    return CampaignCandidate(**values)  # type: ignore[arg-type]


def judged(**overrides: object):
    """Evaluate one candidate at a moment inside its window."""

    now = overrides.pop("now", datetime(2026, 8, 28, 10, 0, tzinfo=UTC))
    regular = overrides.pop("regular_price", REGULAR)
    ambiguous = overrides.pop("regular_price_ambiguous", False)
    return evaluate_candidate(
        key=KEY,
        candidate=campaign(**overrides),
        now=now,  # type: ignore[arg-type]
        regular_price=regular,  # type: ignore[arg-type]
        regular_price_ambiguous=ambiguous,  # type: ignore[arg-type]
    )


# --- BR-005: date and time are the primary eligibility rule -----------------


def test_same_day_window_is_inclusive_of_its_bounds() -> None:
    """Date and time drive eligibility (reference section 4.1)."""

    assert is_within_window(
        datetime(2026, 8, 28, 7, 0, tzinfo=UTC),
        date(2026, 8, 28),
        date(2026, 8, 30),
        time(7, 0),
        time(23, 0),
    )
    assert not is_within_window(
        datetime(2026, 8, 28, 6, 59, tzinfo=UTC),
        date(2026, 8, 28),
        date(2026, 8, 30),
        time(7, 0),
        time(23, 0),
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 8, 28, 22, 30, tzinfo=UTC), True),
        (datetime(2026, 8, 29, 1, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 29, 12, 0, tzinfo=UTC), False),
        (datetime(2026, 8, 31, 1, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 31, 22, 30, tzinfo=UTC), False),
    ],
)
def test_window_crossing_midnight(moment: datetime, expected: bool) -> None:
    """A 22:00 to 02:00 campaign runs into the following day (reference 4.1)."""

    assert (
        is_within_window(
            moment, date(2026, 8, 28), date(2026, 8, 30), time(22, 0), time(2, 0)
        )
        is expected
    )


def test_campaign_outside_its_window_is_ineligible() -> None:
    """An out-of-window campaign yields no promotion rather than an anomaly."""

    evidence = judged(now=datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    assert evidence.eligibility is CandidateEligibility.INELIGIBLE
    assert "OUTSIDE_DATE_TIME_WINDOW" in evidence.reason_codes


def test_campaign_status_is_not_an_input() -> None:
    """Status is not the target authority, so the contract carries no status."""

    assert not hasattr(campaign(), "status")


# --- BR-012: explicit PFS exclusion, never a generic MEMBER filter ----------


def test_pfs_is_excluded_explicitly() -> None:
    """PFS promotions must not reach an ESL (reference section 10)."""

    assert is_pfs_excluded("PFS", None) is True
    assert is_pfs_excluded(None, "PFS PROMO|MEMBER ONLY") is True
    evidence = judged(campaign_group="PFS")
    assert evidence.eligibility is CandidateEligibility.INELIGIBLE
    assert "PFS_EXCLUDED" in evidence.reason_codes


def test_member_alone_is_not_filtered() -> None:
    """A generic MEMBER filter is not approved, so it must not be applied."""

    assert is_pfs_excluded("MEMBER", "MEMBER PRICE|ALL ITEM") is False
    assert judged(campaign_group="MEMBER").eligibility is CandidateEligibility.ELIGIBLE


# --- BR-006: category-001 regular price ------------------------------------


def test_missing_category_001_price_is_unresolved() -> None:
    """No other price category may be substituted (reference section 5)."""

    evidence = judged(regular_price=None)
    assert evidence.eligibility is CandidateEligibility.UNRESOLVED
    assert "CATEGORY_001_PRICE_MISSING" in evidence.reason_codes
    assert evidence.calculated_effective_price is None


def test_ambiguous_category_001_price_is_unresolved() -> None:
    """An ambiguous regular price is a data-quality exception, not a guess."""

    evidence = judged(regular_price_ambiguous=True)
    assert evidence.eligibility is CandidateEligibility.UNRESOLVED
    assert "CATEGORY_001_PRICE_AMBIGUOUS" in evidence.reason_codes


# --- BR-014: structured value validation ------------------------------------


def test_percent_promotion_computes_its_effective_price() -> None:
    """74,500 at 50% becomes 37,250 (reference section 6.1)."""

    assert calculate_effective_price(
        PromotionType.PERCENT, Decimal(50), REGULAR
    ) == Decimal(37250)


@pytest.mark.parametrize("value", [Decimal(0), Decimal(-5)])
def test_non_positive_percent_is_rejected(value: Decimal) -> None:
    """percent <= 0 is not a valid structured promotion (reference 6.1)."""

    evidence = judged(structured_value=value)
    assert evidence.eligibility is CandidateEligibility.REJECTED
    assert "INVALID_PERCENT_VALUE" in evidence.reason_codes


@pytest.mark.parametrize("value", [Decimal(0), Decimal(-1)])
def test_non_positive_fixed_price_is_rejected(value: Decimal) -> None:
    """promo_price <= 0 is not a valid structured promotion (reference 6.2)."""

    evidence = judged(promotion_type=PromotionType.FIXED_PRICE, structured_value=value)
    assert evidence.eligibility is CandidateEligibility.REJECTED
    assert "INVALID_FIXED_PRICE_VALUE" in evidence.reason_codes


def test_fixed_price_above_regular_is_no_promotion() -> None:
    """A fixed price strictly above regular is treated as no promotion (6.2)."""

    evidence = judged(
        promotion_type=PromotionType.FIXED_PRICE, structured_value=Decimal(80000)
    )
    assert evidence.eligibility is CandidateEligibility.INELIGIBLE
    assert "FIXED_PRICE_ABOVE_REGULAR" in evidence.reason_codes


def test_fixed_price_equal_to_regular_is_not_rejected() -> None:
    """The comparison is strictly greater-than (reference section 6.2)."""

    evidence = judged(
        promotion_type=PromotionType.FIXED_PRICE, structured_value=REGULAR
    )
    assert evidence.eligibility is CandidateEligibility.ELIGIBLE
    assert evidence.calculated_effective_price == REGULAR


def test_value_based_promotion_is_not_converted() -> None:
    """No generic value-based conversion exists, so it must not be approximated."""

    evidence = judged(
        promotion_type=PromotionType.VALUE_BASED,
        structured_value=Decimal(60000),
        raw_disc_text="SAVE 60,000 PER CTN",
    )
    assert evidence.eligibility is CandidateEligibility.UNRESOLVED
    assert "VALUE_BASED_CONVERSION_REQUIRED" in evidence.reason_codes


# --- BR-013: UOM resolution -------------------------------------------------


def test_clr_normalizes_to_the_actual_selling_uom() -> None:
    """CLR is not a literal UOM (reference section 11.2)."""

    assert resolve_selling_uom("CLR", "KGS") == "KGS"
    evidence = judged(campaign_uom="CLR")
    assert evidence.resolved_selling_uom == "KGS"
    assert evidence.eligibility is CandidateEligibility.ELIGIBLE


def test_non_clr_mismatch_never_invents_a_conversion() -> None:
    """CTN against PCS requires an authoritative rule (reference 11.3)."""

    assert resolve_selling_uom("CTN", "PCS") is None
    evidence = evaluate_candidate(
        key=CanonicalKey("084", "101011000333", "PCS"),
        candidate=campaign(campaign_uom="CTN", raw_disc_text="SAVE 60,000 PER CTN"),
        now=datetime(2026, 8, 28, 10, 0, tzinfo=UTC),
        regular_price=REGULAR,
        regular_price_ambiguous=False,
    )
    assert evidence.eligibility is CandidateEligibility.UNRESOLVED
    assert "UOM_RULE_REQUIRED" in evidence.reason_codes
    assert evidence.resolved_selling_uom is None
    assert evidence.calculated_effective_price is None


# --- BR-015: scalable display transformation happens after economics --------


def test_kgs_display_price_is_per_100gr_after_calculation() -> None:
    """50,000/KG displays as 5,000/100GR (reference section 12, BR-004)."""

    assert to_display_price("KGS", Decimal(50000)) == Decimal(5000)
    assert to_display_price("PCS", Decimal(50000)) == Decimal(50000)

    evidence = judged()
    assert evidence.calculated_effective_price == Decimal(37250)
    assert evidence.display_price == Decimal(3725)


# --- BR-017: weekday metadata ----------------------------------------------


def test_missing_weekday_metadata_stays_eligible_by_fallback() -> None:
    """Compatibility fallback is preserved and recorded (reference 13)."""

    evidence = judged(weekday=WeekdayEvidence.MISSING)
    assert evidence.eligibility is CandidateEligibility.ELIGIBLE
    assert "MISSING_WEEKDAY_METADATA" in evidence.fallback_codes


def test_explicitly_inactive_weekday_is_distinct_from_missing() -> None:
    """Metadata saying inactive today is not the same as absent metadata."""

    evidence = judged(weekday=WeekdayEvidence.INACTIVE)
    assert evidence.eligibility is CandidateEligibility.INELIGIBLE
    assert "WEEKDAY_INACTIVE" in evidence.reason_codes
    assert "MISSING_WEEKDAY_METADATA" not in evidence.fallback_codes


# --- BR-011: raw DISC_TEXT is preserved, never parsed for logic -------------


def test_raw_disc_text_is_preserved_verbatim() -> None:
    """Manual text is retained for audit and never drives the decision."""

    text = "BELI 2 GRATIS 1 | PROMO   AKHIR PEKAN "
    assert judged(raw_disc_text=text).raw_disc_text == text


def test_promotion_type_is_not_inferred_from_text() -> None:
    """Free-form text must not override the structured type (reference 6)."""

    evidence = judged(
        promotion_type=PromotionType.PERCENT, raw_disc_text="FIXED PRICE 10000"
    )
    assert evidence.promotion_type is PromotionType.PERCENT
    assert evidence.calculated_effective_price == Decimal(37250)


# --- BR-016/BR-018: atomic state, one candidate, one key --------------------


def test_single_eligible_candidate_produces_an_atomic_state() -> None:
    """Every promotion field comes from exactly one candidate (reference 16)."""

    evidence = judged()
    evaluation = evaluate(KEY, "rules-v1", "calc-v1", (evidence,))

    assert evaluation.outcome is PromotionOutcome.SELECTED
    assert evaluation.selected_candidate_id == evidence.candidate_id
    state = evaluation.resulting_state
    assert state is not None
    assert state.source_campaign_id == "A"
    assert state.effective_price == Decimal(37250)
    assert state.display_price == Decimal(3725)
    assert state.raw_disc_text == evidence.raw_disc_text


def test_several_eligible_candidates_are_ambiguous_without_a_winner() -> None:
    """No winner is chosen: priority is unresolved and belongs to #37."""

    first = judged(campaign_id="A")
    second = judged(campaign_id="B", structured_value=Decimal(40))
    evaluation = evaluate(KEY, "rules-v1", "calc-v1", (first, second))

    assert evaluation.outcome is PromotionOutcome.AMBIGUOUS
    assert evaluation.selected_candidate_id is None
    assert evaluation.resulting_state is None
    assert {item.source_campaign_id for item in evaluation.candidates} == {"A", "B"}


def test_no_candidate_means_no_promotion() -> None:
    """An item with no campaign has an explicit outcome, not a null."""

    assert evaluate(KEY, "rules-v1", "calc-v1", ()).outcome is (
        PromotionOutcome.NO_PROMOTION
    )


def test_unresolved_outweighs_rejected_and_ineligible() -> None:
    """An unresolved candidate blocks the scope rather than being dropped."""

    unresolved = judged(campaign_id="U", campaign_uom="CTN")
    rejected = judged(campaign_id="R", structured_value=Decimal(0))
    evaluation = evaluate(KEY, "rules-v1", "calc-v1", (rejected, unresolved))

    assert evaluation.outcome is PromotionOutcome.UNRESOLVED
    assert evaluation.resulting_state is None


def test_only_rejected_candidates_produce_a_rejected_outcome() -> None:
    """Invalid structured values are a data-quality outcome."""

    rejected = judged(campaign_id="R", structured_value=Decimal(-1))
    assert evaluate(KEY, "rules-v1", "calc-v1", (rejected,)).outcome is (
        PromotionOutcome.REJECTED
    )


def test_candidates_may_not_cross_the_canonical_key() -> None:
    """Store and selling UOM bound every evaluation (reference 17, BR-018)."""

    other_store = evaluate_candidate(
        key=CanonicalKey("075", "101024011793", "KGS"),
        candidate=campaign(campaign_id="B"),
        now=datetime(2026, 8, 28, 10, 0, tzinfo=UTC),
        regular_price=REGULAR,
        regular_price_ambiguous=False,
    )
    with pytest.raises(ValueError, match="canonical key"):
        evaluate(KEY, "rules-v1", "calc-v1", (judged(), other_store))
