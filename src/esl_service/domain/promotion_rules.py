"""Reference-directed promotion rules, independently testable and pure.

Every rule traces to the approved
``docs/sql-server/ESL_Promotion_Business_Logic_and_Business_Rules_Reference.md``
and to BR-011 through BR-018. Nothing here contacts SQL Server, AIMS, or a
database, so each rule is testable in isolation (FR-005).

Two things are deliberately absent, because both remain
UNKNOWN / NEEDS-DISCOVERY:

* choosing between several eligible campaigns (campaign priority, #37);
* converting a non-CLR campaign UOM, or parsing ``DISC_TEXT`` for logic.

Deployed legacy parity is not claimed by these rules; #38 is that gate.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from esl_service.domain.canonical import CanonicalKey, PromotionStateData
from esl_service.domain.promotion_evidence import (
    FALLBACK_MISSING_WEEKDAY_METADATA,
    REASON_FIXED_PRICE_ABOVE_REGULAR,
    REASON_INVALID_FIXED_PRICE,
    REASON_INVALID_PERCENT,
    REASON_OUTSIDE_WINDOW,
    REASON_PFS_EXCLUDED,
    REASON_PRICE_AMBIGUOUS,
    REASON_PRICE_MISSING,
    REASON_UOM_RULE_REQUIRED,
    REASON_VALUE_BASED_UNSUPPORTED,
    REASON_WEEKDAY_INACTIVE,
    CandidateEligibility,
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)

#: Campaign UOM meaning "the item's own selling UOM" (reference section 11.2).
CLR_UOM = "CLR"

#: Selling UOM whose ESL display price is per 100 grams (BR-004, BR-015).
SCALABLE_UOM = "KGS"

#: Divisor converting a per-kilogram price to a per-100-gram price.
PER_100GR_DIVISOR = Decimal(10)

#: Explicit exclusion token. A generic MEMBER filter is NOT approved (BR-012).
PFS_TOKEN = "PFS"


@dataclass(frozen=True)
class CampaignCandidate:
    """One source campaign as supplied, before any rule is applied.

    Campaign status is intentionally absent: it is not the target eligibility
    authority (BR-005, reference section 4.2).
    """

    campaign_id: str
    campaign_group: str | None
    promotion_type: PromotionType
    structured_value: Decimal
    raw_disc_text: str | None
    start_date: date
    end_date: date
    start_time: time
    end_time: time
    campaign_uom: str
    weekday: WeekdayEvidence


def is_within_window(
    now: datetime,
    start_date: date,
    end_date: date,
    start_time: time,
    end_time: time,
) -> bool:
    """Return whether a moment falls inside a campaign's date and time window.

    Date and time are the primary eligibility rule (reference section 4.1).
    Bounds are inclusive, matching the source ``BETWEEN`` semantics. A window
    whose start time is after its end time crosses midnight: it runs from the
    start time on each campaign day until the end time on the following day.
    """

    current_date = now.date()
    current_time = now.time()

    if start_time <= end_time:
        return start_date <= current_date <= end_date and (
            start_time <= current_time <= end_time
        )

    evening = start_date <= current_date <= end_date and current_time >= start_time
    morning = (
        start_date + timedelta(days=1) <= current_date <= end_date + timedelta(days=1)
        and current_time <= end_time
    )
    return evening or morning


def is_pfs_excluded(campaign_group: str | None, raw_disc_text: str | None) -> bool:
    """Return whether a campaign is an excluded PFS/member promotion (BR-012).

    The exclusion is explicit and token-based. A generic ``MEMBER`` filter is
    not applied, because the business has not approved one.
    """

    return any(
        PFS_TOKEN in value.upper()
        for value in (campaign_group, raw_disc_text)
        if value is not None
    )


def resolve_selling_uom(campaign_uom: str, selling_uom: str) -> str | None:
    """Resolve a campaign UOM against the item's actual selling UOM (BR-013).

    ``CLR`` normalises to the selling UOM. A different, non-``CLR`` UOM has no
    authoritative conversion, so ``None`` is returned rather than a guess.
    """

    normalized = campaign_uom.strip().upper()
    if normalized == CLR_UOM or normalized == selling_uom.strip().upper():
        return selling_uom
    return None


def calculate_effective_price(
    promotion_type: PromotionType, structured_value: Decimal, regular_price: Decimal
) -> Decimal | None:
    """Return the promotion's effective price on the selling UOM basis.

    Percent and fixed-price promotions are defined in reference section 6.
    Value-based promotions have no generic conversion and return ``None``; no
    rounding policy is applied, because none is approved.
    """

    if promotion_type is PromotionType.PERCENT:
        return regular_price - (regular_price * structured_value / Decimal(100))
    if promotion_type is PromotionType.FIXED_PRICE:
        return structured_value
    return None


def to_display_price(selling_uom: str, price: Decimal) -> Decimal:
    """Convert a selling-UOM price to its ESL display value (BR-004, BR-015).

    The transformation happens only after the economics are calculated on the
    selling UOM, never by evaluating the campaign against ``/100GR`` directly.
    """

    if selling_uom.strip().upper() == SCALABLE_UOM:
        return price / PER_100GR_DIVISOR
    return price


def evaluate_candidate(
    *,
    key: CanonicalKey,
    candidate: CampaignCandidate,
    now: datetime,
    regular_price: Decimal | None,
    regular_price_ambiguous: bool = False,
) -> PromotionCandidateEvidence:
    """Apply every reference-directed rule to one campaign and record why.

    The candidate is always returned as evidence, whether usable or not, so a
    rejection or unresolved condition stays observable (FR-006, FR-022).
    """

    reasons: list[str] = []
    fallbacks: list[str] = []
    eligibility = CandidateEligibility.ELIGIBLE

    if not is_within_window(
        now,
        candidate.start_date,
        candidate.end_date,
        candidate.start_time,
        candidate.end_time,
    ):
        reasons.append(REASON_OUTSIDE_WINDOW)
        eligibility = CandidateEligibility.INELIGIBLE

    if is_pfs_excluded(candidate.campaign_group, candidate.raw_disc_text):
        reasons.append(REASON_PFS_EXCLUDED)
        eligibility = CandidateEligibility.INELIGIBLE

    if candidate.weekday is WeekdayEvidence.INACTIVE:
        reasons.append(REASON_WEEKDAY_INACTIVE)
        eligibility = CandidateEligibility.INELIGIBLE
    elif candidate.weekday is WeekdayEvidence.MISSING:
        # Compatibility fallback: eligible by date/time, recorded for
        # monitoring until the business decides otherwise (BR-017).
        fallbacks.append(FALLBACK_MISSING_WEEKDAY_METADATA)

    resolved_uom = resolve_selling_uom(candidate.campaign_uom, key.selling_uom)
    if resolved_uom is None:
        reasons.append(REASON_UOM_RULE_REQUIRED)
        eligibility = CandidateEligibility.UNRESOLVED

    if regular_price_ambiguous:
        reasons.append(REASON_PRICE_AMBIGUOUS)
        eligibility = CandidateEligibility.UNRESOLVED
    elif regular_price is None:
        reasons.append(REASON_PRICE_MISSING)
        eligibility = CandidateEligibility.UNRESOLVED

    if candidate.promotion_type is PromotionType.PERCENT:
        if candidate.structured_value <= 0:
            reasons.append(REASON_INVALID_PERCENT)
            eligibility = CandidateEligibility.REJECTED
    elif candidate.promotion_type is PromotionType.FIXED_PRICE:
        if candidate.structured_value <= 0:
            reasons.append(REASON_INVALID_FIXED_PRICE)
            eligibility = CandidateEligibility.REJECTED
        elif regular_price is not None and candidate.structured_value > regular_price:
            # Strictly greater only; equal is not rejected (reference 6.2).
            reasons.append(REASON_FIXED_PRICE_ABOVE_REGULAR)
            eligibility = CandidateEligibility.INELIGIBLE
    else:
        reasons.append(REASON_VALUE_BASED_UNSUPPORTED)
        eligibility = CandidateEligibility.UNRESOLVED

    effective_price: Decimal | None = None
    display_price: Decimal | None = None
    if eligibility is CandidateEligibility.ELIGIBLE and regular_price is not None:
        effective_price = calculate_effective_price(
            candidate.promotion_type, candidate.structured_value, regular_price
        )
        if effective_price is not None:
            display_price = to_display_price(key.selling_uom, effective_price)

    return PromotionCandidateEvidence(
        candidate_id=candidate.campaign_id,
        key=key,
        source_campaign_id=candidate.campaign_id,
        campaign_group=candidate.campaign_group,
        promotion_type=candidate.promotion_type,
        structured_value=candidate.structured_value,
        raw_disc_text=candidate.raw_disc_text,
        starts_at=datetime.combine(
            candidate.start_date, candidate.start_time, tzinfo=now.tzinfo
        ),
        ends_at=datetime.combine(
            candidate.end_date, candidate.end_time, tzinfo=now.tzinfo
        ),
        weekday_evidence=candidate.weekday,
        category_001_regular_price=regular_price,
        source_uom=candidate.campaign_uom,
        resolved_selling_uom=resolved_uom,
        calculated_effective_price=effective_price,
        display_price=display_price,
        eligibility=eligibility,
        reason_codes=tuple(reasons),
        fallback_codes=tuple(fallbacks),
    )


def evaluate(
    key: CanonicalKey,
    rule_version: str,
    calculation_version: str,
    candidates: tuple[PromotionCandidateEvidence, ...],
) -> PromotionEvaluationEvidence:
    """Combine candidate evidence into one outcome without choosing a winner.

    Exactly one eligible candidate yields an atomic SELECTED state (BR-016).
    Several eligible candidates are AMBIGUOUS and retain every candidate: the
    priority rule is unresolved, so this function must not pick one (#37).

    With no eligible candidate, an unresolved condition outranks a rejected
    one, because unresolved work must block rather than be silently dropped.
    """

    eligible = [
        item for item in candidates if item.eligibility is CandidateEligibility.ELIGIBLE
    ]

    if len(eligible) == 1:
        selected = eligible[0]
        return PromotionEvaluationEvidence(
            key=key,
            rule_version=rule_version,
            calculation_version=calculation_version,
            outcome=PromotionOutcome.SELECTED,
            candidates=candidates,
            selected_candidate_id=selected.candidate_id,
            resulting_state=build_state(selected),
        )

    if len(eligible) > 1:
        outcome = PromotionOutcome.AMBIGUOUS
    elif any(
        item.eligibility is CandidateEligibility.UNRESOLVED for item in candidates
    ):
        outcome = PromotionOutcome.UNRESOLVED
    elif any(item.eligibility is CandidateEligibility.REJECTED for item in candidates):
        outcome = PromotionOutcome.REJECTED
    else:
        outcome = PromotionOutcome.NO_PROMOTION

    return PromotionEvaluationEvidence(
        key=key,
        rule_version=rule_version,
        calculation_version=calculation_version,
        outcome=outcome,
        candidates=candidates,
        selected_candidate_id=None,
        resulting_state=None,
    )


def build_state(candidate: PromotionCandidateEvidence) -> PromotionStateData:
    """Build the atomic promotion state from exactly one candidate (BR-016)."""

    saving: Decimal | None = None
    if (
        candidate.category_001_regular_price is not None
        and candidate.calculated_effective_price is not None
    ):
        saving = (
            candidate.category_001_regular_price - candidate.calculated_effective_price
        )

    return PromotionStateData(
        source_campaign_id=candidate.source_campaign_id,
        promotion_flag=None,
        promotion_type=candidate.promotion_type.value,
        campaign_group=candidate.campaign_group,
        structured_value=candidate.structured_value,
        effective_price=candidate.calculated_effective_price,
        display_price=candidate.display_price,
        discount_percentage=(
            candidate.structured_value
            if candidate.promotion_type is PromotionType.PERCENT
            else None
        ),
        saving_amount=saving,
        raw_disc_text=candidate.raw_disc_text,
        starts_at=candidate.starts_at,
        ends_at=candidate.ends_at,
    )
