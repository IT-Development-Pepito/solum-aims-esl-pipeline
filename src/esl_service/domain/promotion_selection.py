"""Conservative promotion compatibility selection (BR-019, BR-020).

Promotion priority is **not formally defined** (reference section 7.1), so this
module invents none. It contains no lowest-price, fixed-price, percent,
newest-campaign, or clearance rule, and adding one requires an approved
business decision rather than a code change.

Selection happens only where selecting is not a business decision:

1. **one eligible candidate** — nothing to choose between;
2. **the current state already matches** an eligible candidate's outbound
   state — retaining it changes nothing on the label, so no new interpretation
   is made;
3. **every eligible candidate produces an identical outbound state** — the
   choice cannot affect what is displayed. "Identical" compares every field of
   the promotion state except the campaign identity itself: two campaign codes
   carrying the same type, value, prices, percentage, saving, raw text, group,
   and period display and price the same, so picking either changes nothing a
   shopper or a term could observe. Any difference in those fields, including
   the promotion period, keeps the outcome unresolved under reference 8.

Anything else stays UNRESOLVED with its ambiguity recorded:

* different calculated economic outcomes → ``PROMO_PRIORITY_DIFFERENT_ECONOMIC``
  (reference 7.3);
* equal calculated economic outcomes with different terms or display →
  ``DISPLAY_PRIORITY_SAME_ECONOMIC`` (reference 8).

Comparison uses the **calculated** effective price as exact ``Decimal`` values,
never raw type, price, or percent, and applies **no rounding**, because no
rounding policy is approved. The calculation version travels with the
evaluation so a future approved rule is a versioned replacement.

Ambiguity remains observable even when a candidate is chosen, as reference 7.3
requires. Deployed compatibility is **not** claimed: #38 and representative
cases remain required before any such claim.
"""

from dataclasses import replace
from decimal import Decimal

from esl_service.domain.canonical import PromotionStateData
from esl_service.domain.promotion_evidence import (
    CandidateEligibility,
    PromotionCandidateEvidence,
    PromotionEvaluationEvidence,
    PromotionOutcome,
)
from esl_service.domain.promotion_rules import build_state

#: Names the strategy so an approved priority rule replaces a known version.
SELECTION_STRATEGY_VERSION = "compatibility-v1"

#: Several eligible campaigns produce different calculated outcomes (7.3).
REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC = "PROMO_PRIORITY_DIFFERENT_ECONOMIC"

#: Equal calculated outcomes, but the campaign terms differ (reference 8).
REASON_DISPLAY_PRIORITY_SAME_ECONOMIC = "DISPLAY_PRIORITY_SAME_ECONOMIC"


def select_compatibility_state(
    evaluation: PromotionEvaluationEvidence,
    *,
    existing_state: PromotionStateData | None = None,
) -> PromotionEvaluationEvidence:
    """Apply the conservative strategy, recording ambiguity either way.

    Returns the evaluation unchanged when there is nothing to decide, and
    never converts an unresolved or rejected outcome into a selection.
    """

    eligible = tuple(
        item
        for item in evaluation.candidates
        if item.eligibility is CandidateEligibility.ELIGIBLE
    )
    if len(eligible) < 2:
        # No competition: #36 already reached the only possible outcome, and a
        # rejected or unresolved evaluation must not be rescued here.
        return evaluation

    ambiguity = _ambiguity_code(eligible)
    candidates = _record_ambiguity(evaluation.candidates, eligible, ambiguity)

    chosen = _safe_choice(eligible, existing_state)
    if chosen is None:
        return replace(
            evaluation,
            outcome=PromotionOutcome.UNRESOLVED,
            candidates=candidates,
            selected_candidate_id=None,
            resulting_state=None,
        )

    return replace(
        evaluation,
        outcome=PromotionOutcome.SELECTED,
        candidates=candidates,
        selected_candidate_id=chosen.candidate_id,
        resulting_state=build_state(chosen),
    )


def _ambiguity_code(eligible: tuple[PromotionCandidateEvidence, ...]) -> str:
    """Classify why several candidates are eligible at once."""

    prices = {_effective(item) for item in eligible}
    if len(prices) > 1:
        return REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC
    return REASON_DISPLAY_PRIORITY_SAME_ECONOMIC


def _safe_choice(
    eligible: tuple[PromotionCandidateEvidence, ...],
    existing_state: PromotionStateData | None,
) -> PromotionCandidateEvidence | None:
    """Return a candidate only when choosing it is not a business decision."""

    ordered = sorted(eligible, key=lambda item: item.source_campaign_id)

    if existing_state is not None:
        # Retaining a state the label already shows changes nothing.
        for item in ordered:
            if build_state(item) == existing_state:
                return item

    states = [_comparable_state(item) for item in ordered]
    if all(state == states[0] for state in states):
        # Deterministic by campaign code, so the same input always yields the
        # same choice; the states are equivalent, so the choice is not a
        # business decision.
        return ordered[0]
    return None


def _comparable_state(candidate: PromotionCandidateEvidence) -> PromotionStateData:
    """Return the outbound state with the campaign identity removed.

    Campaign identity is recorded in the state for traceability, but it is not
    something a shopper or a campaign term can observe, so it must not by
    itself make two otherwise identical states look different.
    """

    return replace(build_state(candidate), source_campaign_id="")


def _record_ambiguity(
    candidates: tuple[PromotionCandidateEvidence, ...],
    eligible: tuple[PromotionCandidateEvidence, ...],
    code: str,
) -> tuple[PromotionCandidateEvidence, ...]:
    """Attach the ambiguity code to every eligible candidate."""

    eligible_ids = {item.candidate_id for item in eligible}
    return tuple(
        replace(item, reason_codes=(*item.reason_codes, code))
        if item.candidate_id in eligible_ids and code not in item.reason_codes
        else item
        for item in candidates
    )


def _effective(candidate: PromotionCandidateEvidence) -> Decimal:
    """Return one candidate's calculated effective price for comparison.

    Compared exactly and unrounded: no rounding policy is approved, so
    rounding here would silently create equality that does not exist.
    """

    price = candidate.calculated_effective_price
    if price is None:
        raise ValueError(
            "an eligible candidate must carry a calculated effective price"
        )
    return price
