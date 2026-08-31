"""Immutable evidence of what a promotion evaluation considered and why.

These contracts retain every candidate, its calculated values, and the reasons
it was or was not usable, so a decision stays auditable and replayable
(FR-004, FR-022, BR-016, BR-018, BR-019).

This module deliberately contains **no winner-selection policy**. Campaign
priority for several eligible candidates is UNKNOWN / NEEDS-DISCOVERY, and
issue #37 consumes these contracts once an approved rule exists.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from esl_service.domain.canonical import CanonicalKey, PromotionStateData


class PromotionType(StrEnum):
    """Structured promotion types recognised by the source process."""

    PERCENT = "PERCENT"
    FIXED_PRICE = "FIXED_PRICE"
    VALUE_BASED = "VALUE_BASED"


class PromotionOutcome(StrEnum):
    """The evaluated outcome for one canonical key."""

    NO_PROMOTION = "NO_PROMOTION"
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


class CandidateEligibility(StrEnum):
    """Whether one candidate can be used, and if not, why not."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


class WeekdayEvidence(StrEnum):
    """Warehouse weekday metadata, keeping absent distinct from inactive."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    MISSING = "MISSING"


# Reason and fallback codes. These are identifiers for conditions the approved
# reference describes; they encode no priority or conversion policy.
REASON_OUTSIDE_WINDOW = "OUTSIDE_DATE_TIME_WINDOW"
REASON_PFS_EXCLUDED = "PFS_EXCLUDED"
REASON_WEEKDAY_INACTIVE = "WEEKDAY_INACTIVE"
REASON_PRICE_MISSING = "CATEGORY_001_PRICE_MISSING"
REASON_PRICE_AMBIGUOUS = "CATEGORY_001_PRICE_AMBIGUOUS"
REASON_INVALID_PERCENT = "INVALID_PERCENT_VALUE"
REASON_INVALID_FIXED_PRICE = "INVALID_FIXED_PRICE_VALUE"
REASON_FIXED_PRICE_ABOVE_REGULAR = "FIXED_PRICE_ABOVE_REGULAR"
REASON_UOM_RULE_REQUIRED = "UOM_RULE_REQUIRED"
REASON_VALUE_BASED_UNSUPPORTED = "VALUE_BASED_CONVERSION_REQUIRED"
FALLBACK_MISSING_WEEKDAY_METADATA = "MISSING_WEEKDAY_METADATA"


@dataclass(frozen=True)
class PromotionCandidateEvidence:
    """One campaign considered for one canonical key, with its calculation."""

    candidate_id: str
    key: CanonicalKey
    source_campaign_id: str
    campaign_group: str | None
    promotion_type: PromotionType
    structured_value: Decimal
    raw_disc_text: str | None
    starts_at: datetime
    ends_at: datetime
    weekday_evidence: WeekdayEvidence
    category_001_regular_price: Decimal | None
    source_uom: str
    resolved_selling_uom: str | None
    calculated_effective_price: Decimal | None
    display_price: Decimal | None
    eligibility: CandidateEligibility
    reason_codes: tuple[str, ...]
    fallback_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id must not be blank")
        if not self.source_campaign_id.strip():
            raise ValueError("source_campaign_id must not be blank")


@dataclass(frozen=True)
class PromotionEvaluationEvidence:
    """The complete evaluation for one canonical key at one rule version."""

    key: CanonicalKey
    rule_version: str
    calculation_version: str
    outcome: PromotionOutcome
    candidates: tuple[PromotionCandidateEvidence, ...]
    selected_candidate_id: str | None
    resulting_state: PromotionStateData | None

    def __post_init__(self) -> None:
        identifiers = [item.candidate_id for item in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("candidate identifiers must be unique")
        for item in self.candidates:
            if item.key != self.key:
                raise ValueError(
                    "a candidate may not cross the evaluation canonical key"
                )

        if self.outcome is not PromotionOutcome.SELECTED:
            if self.selected_candidate_id is not None:
                raise ValueError("only a SELECTED outcome may name a candidate")
            if self.resulting_state is not None:
                raise ValueError("only a SELECTED outcome may carry a promotion state")
            return

        selected = self.selected_candidate()
        if selected is None:
            raise ValueError("selected candidate must be one of the candidates")
        if self.resulting_state is None:
            raise ValueError("a SELECTED outcome must carry a promotion state")
        self._require_atomic_state(selected, self.resulting_state)

    def selected_candidate(self) -> PromotionCandidateEvidence | None:
        """Return the candidate the resulting state was built from."""

        return next(
            (
                item
                for item in self.candidates
                if item.candidate_id == self.selected_candidate_id
            ),
            None,
        )

    @staticmethod
    def _require_atomic_state(
        candidate: PromotionCandidateEvidence, state: PromotionStateData
    ) -> None:
        """Every promotion field must come from one candidate (BR-016)."""

        mismatched = (
            state.source_campaign_id != candidate.source_campaign_id
            or state.promotion_type != candidate.promotion_type.value
            or state.campaign_group != candidate.campaign_group
            or state.structured_value != candidate.structured_value
            or state.effective_price != candidate.calculated_effective_price
            or state.display_price != candidate.display_price
            or state.raw_disc_text != candidate.raw_disc_text
            or state.starts_at != candidate.starts_at
            or state.ends_at != candidate.ends_at
        )
        if mismatched:
            raise ValueError(
                "resulting_state must be atomic: every field from one candidate"
            )
