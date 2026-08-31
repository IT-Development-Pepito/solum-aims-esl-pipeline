"""Structural source-record validation and quarantine (FR-003, FR-006).

Validation here is structural only: required identity and reproducibility
fields, correct types, and canonical-key uniqueness within a batch. Value-level
range and domain thresholds are UNKNOWN / NEEDS-DISCOVERY and are tracked by
issue #58; none is invented here.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ProcessingStatus,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import (
    PromotionOutcome,
    PromotionType,
    WeekdayEvidence,
)
from esl_service.domain.promotion_rules import (
    CampaignCandidate,
    evaluate,
    evaluate_candidate,
)
from esl_service.domain.validation import (
    ISSUE_DUPLICATE_CANONICAL_KEY,
    ISSUE_MISSING_REQUIRED_FIELD,
    assess_record,
    promotion_issues,
    validate_batch,
)
from tests.factories import canonical_record

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


def campaign(**overrides: object) -> CampaignCandidate:
    """Build a campaign candidate for promotion evaluation."""

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


def evaluation_of(*candidates: CampaignCandidate, regular: Decimal | None = Decimal(74500)):
    """Evaluate campaigns for the default canonical key."""

    key = CanonicalKey("084", "101024011793", "KGS")
    evidences = tuple(
        evaluate_candidate(
            key=key,
            candidate=item,
            now=NOW,
            regular_price=regular,
            regular_price_ambiguous=False,
        )
        for item in candidates
    )
    return evaluate(key, "rules-v1", "calc-v1", evidences)


# --- structural validation --------------------------------------------------


def test_a_complete_record_produces_no_issue() -> None:
    """A well-formed canonical record passes structural validation."""

    results = validate_batch((canonical_record(),))
    assert results[0].issues == ()
    assert results[0].validation_status is ValidationStatus.VALID


def test_duplicate_canonical_key_is_rejected() -> None:
    """Key uniqueness is enforced within a batch (FR-003, BR-018)."""

    duplicated = (canonical_record(), canonical_record())
    results = validate_batch(duplicated)

    assert results[0].validation_status is ValidationStatus.VALID
    assert results[1].validation_status is ValidationStatus.REJECTED
    assert [item.issue_code for item in results[1].issues] == [
        ISSUE_DUPLICATE_CANONICAL_KEY
    ]


def test_same_item_in_two_stores_is_not_a_duplicate() -> None:
    """Store context is part of the key, so two stores never collide."""

    results = validate_batch(
        (canonical_record(store_code="075"), canonical_record(store_code="084"))
    )
    assert [item.validation_status for item in results] == [
        ValidationStatus.VALID,
        ValidationStatus.VALID,
    ]


def test_same_item_in_two_selling_uoms_is_not_a_duplicate() -> None:
    """Selling UOM is part of the key (BR-018)."""

    results = validate_batch(
        (canonical_record(selling_uom="KGS"), canonical_record(selling_uom="PCS"))
    )
    assert all(item.validation_status is ValidationStatus.VALID for item in results)


def test_missing_required_reproducibility_field_is_rejected() -> None:
    """A record that cannot be reproduced later is rejected (FR-004)."""

    record = canonical_record()
    blank = type(record)(
        **{
            **record.__dict__,
            "schema_version": "   ",
        }
    )
    results = validate_batch((blank,))

    assert results[0].validation_status is ValidationStatus.REJECTED
    codes = [item.issue_code for item in results[0].issues]
    assert ISSUE_MISSING_REQUIRED_FIELD in codes
    assert results[0].issues[0].evidence["field"] == "schema_version"


def test_mixed_batch_lets_valid_records_continue() -> None:
    """One bad record never blocks the rest of the batch (FR-006)."""

    results = validate_batch(
        (
            canonical_record(store_code="075"),
            canonical_record(store_code="084"),
            canonical_record(store_code="084"),
        )
    )
    statuses = [item.validation_status for item in results]
    assert statuses == [
        ValidationStatus.VALID,
        ValidationStatus.VALID,
        ValidationStatus.REJECTED,
    ]


def test_raw_disc_text_is_never_rejected_for_its_shape() -> None:
    """Manual text is display/audit input, not a validated structure (BR-011)."""

    from esl_service.domain.canonical import PromotionStateData

    odd_text = "BELI 2 GRATIS 1"  # no pipes, no fixed field count
    record = canonical_record(
        promotion_state=PromotionStateData(
            source_campaign_id="A",
            promotion_flag=None,
            promotion_type="PERCENT",
            campaign_group="REGULAR",
            structured_value=Decimal(50),
            effective_price=Decimal(37250),
            display_price=Decimal(3725),
            discount_percentage=Decimal(50),
            saving_amount=None,
            raw_disc_text=odd_text,
            starts_at=NOW,
            ends_at=NOW,
        )
    )
    results = validate_batch((record,))
    assert results[0].validation_status is ValidationStatus.VALID


# --- promotion anomalies surfaced as record issues --------------------------


def test_unresolved_uom_becomes_a_record_issue() -> None:
    """An unconvertible campaign UOM is deterministic and traceable (BR-013)."""

    evaluation = evaluation_of(campaign(campaign_uom="CTN"))
    issues = promotion_issues(evaluation)

    assert [item.issue_code for item in issues] == ["UOM_RULE_REQUIRED"]
    assert issues[0].rule_id == "BR-013"


def test_missing_regular_price_becomes_a_record_issue() -> None:
    """A missing category-001 price is reported, never substituted (BR-006)."""

    evaluation = evaluation_of(campaign(), regular=None)
    assert "CATEGORY_001_PRICE_MISSING" in [
        item.issue_code for item in promotion_issues(evaluation)
    ]


def test_invalid_promotion_value_becomes_a_record_issue() -> None:
    """A non-positive percent is a deterministic rejection reason (BR-014)."""

    evaluation = evaluation_of(campaign(structured_value=Decimal(0)))
    assert "INVALID_PERCENT_VALUE" in [
        item.issue_code for item in promotion_issues(evaluation)
    ]


def test_fixed_price_above_regular_becomes_a_record_issue() -> None:
    """The safety rule is reported rather than silently dropping the campaign."""

    evaluation = evaluation_of(
        campaign(
            promotion_type=PromotionType.FIXED_PRICE, structured_value=Decimal(80000)
        )
    )
    assert "FIXED_PRICE_ABOVE_REGULAR" in [
        item.issue_code for item in promotion_issues(evaluation)
    ]


def test_missing_weekday_metadata_is_recorded_as_an_issue() -> None:
    """The fallback stays eligible but is monitored (BR-017)."""

    evaluation = evaluation_of(campaign(weekday=WeekdayEvidence.MISSING))
    codes = [item.issue_code for item in promotion_issues(evaluation)]

    assert "MISSING_WEEKDAY_METADATA" in codes
    assert evaluation.outcome is PromotionOutcome.SELECTED


def test_promotion_ambiguity_is_recorded_as_an_issue() -> None:
    """Ambiguity is observable; no winner is chosen here (BR-019)."""

    evaluation = evaluation_of(
        campaign(campaign_id="A"),
        campaign(campaign_id="B", structured_value=Decimal(40)),
    )
    issues = promotion_issues(evaluation)

    assert evaluation.outcome is PromotionOutcome.AMBIGUOUS
    assert "PROMOTION_AMBIGUOUS" in [item.issue_code for item in issues]


# --- assembling the record outcome ------------------------------------------


def test_rejected_record_requests_no_action() -> None:
    """A rejected record never yields an external action (FR-003)."""

    duplicated = validate_batch((canonical_record(), canonical_record()))
    result = assess_record(
        validation=duplicated[1],
        evaluation=None,
        current_page=1,
        desired_page=2,
    )
    assert result.processing_status is ProcessingStatus.REJECTED
    assert result.action_decision is ActionDecision.NONE


def test_unresolved_promotion_requests_no_action() -> None:
    """Unresolved work is quarantined rather than acted on (FR-006)."""

    validation = validate_batch((canonical_record(),))[0]
    result = assess_record(
        validation=validation,
        evaluation=evaluation_of(campaign(campaign_uom="CTN")),
        current_page=1,
        desired_page=2,
    )
    assert result.eligibility_status is EligibilityStatus.UNRESOLVED
    assert result.processing_status is ProcessingStatus.UNRESOLVED
    assert result.action_decision is ActionDecision.NONE
    assert result.issues != ()


def test_unchanged_page_is_skipped_as_idempotent() -> None:
    """No action is requested when the page already matches (BR-008)."""

    validation = validate_batch((canonical_record(),))[0]
    result = assess_record(
        validation=validation,
        evaluation=evaluation_of(campaign()),
        current_page=2,
        desired_page=2,
    )
    assert result.processing_status is ProcessingStatus.UNCHANGED
    assert result.action_decision is ActionDecision.SKIP_IDEMPOTENT


def test_changed_page_requires_an_action() -> None:
    """A valid record with a different desired page requests a page change."""

    validation = validate_batch((canonical_record(),))[0]
    result = assess_record(
        validation=validation,
        evaluation=evaluation_of(campaign()),
        current_page=1,
        desired_page=2,
    )
    assert result.processing_status is ProcessingStatus.ACTION_REQUIRED
    assert result.action_decision is ActionDecision.PAGE_CHANGE
