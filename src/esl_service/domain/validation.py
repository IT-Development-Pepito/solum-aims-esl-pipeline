"""Structural source-record validation and quarantine (FR-003, FR-006).

Validation here is deliberately **structural only**: required identity and
reproducibility fields, and canonical-key uniqueness within a batch. Types are
already enforced by the frozen canonical contracts.

Value-level range and domain thresholds — price, stock, quantity, weight,
expiry, UOM domain, percentage bounds — are **UNKNOWN / NEEDS-DISCOVERY** and
are tracked by issue #58. None is invented here, because no supplied evidence
states them.

Promotion anomalies are not recomputed: they are surfaced from the evidence
:mod:`esl_service.domain.promotion_rules` already produced.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from esl_service.domain.canonical import CanonicalEslRecord, CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ProcessingStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import (
    CandidateEligibility,
    PromotionEvaluationEvidence,
    PromotionOutcome,
)

ISSUE_DUPLICATE_CANONICAL_KEY = "DUPLICATE_CANONICAL_KEY"
ISSUE_MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
ISSUE_PROMOTION_AMBIGUOUS = "PROMOTION_AMBIGUOUS"

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
CLASSIFICATION_VALIDATION = "VALIDATION"
CLASSIFICATION_PROMOTION = "PROMOTION"

#: Rule identifiers for the promotion reason codes produced by #36.
_PROMOTION_RULE_BY_CODE = {
    "OUTSIDE_DATE_TIME_WINDOW": "BR-005",
    "PFS_EXCLUDED": "BR-012",
    "WEEKDAY_INACTIVE": "BR-017",
    "CATEGORY_001_PRICE_MISSING": "BR-006",
    "CATEGORY_001_PRICE_AMBIGUOUS": "BR-006",
    "INVALID_PERCENT_VALUE": "BR-014",
    "INVALID_FIXED_PRICE_VALUE": "BR-014",
    "FIXED_PRICE_ABOVE_REGULAR": "BR-014",
    "UOM_RULE_REQUIRED": "BR-013",
    "VALUE_BASED_CONVERSION_REQUIRED": "BR-014",
    "MISSING_WEEKDAY_METADATA": "BR-017",
    # Ambiguity classifications produced by the #37 compatibility strategy.
    "PROMO_PRIORITY_DIFFERENT_ECONOMIC": "BR-019",
    "DISPLAY_PRIORITY_SAME_ECONOMIC": "BR-019",
}


@dataclass(frozen=True)
class RecordValidation:
    """The structural verdict for one source record."""

    key: CanonicalKey
    validation_status: ValidationStatus
    issues: tuple[RecordIssueEvidence, ...]


def validate_batch(
    records: Sequence[CanonicalEslRecord],
) -> tuple[RecordValidation, ...]:
    """Validate a batch structurally, rejecting only the records at fault.

    A rejected record never blocks the rest of the batch (FR-006). The first
    occurrence of a canonical key is kept and later repeats are rejected, so
    the outcome is deterministic for a given input order.
    """

    seen: set[CanonicalKey] = set()
    results: list[RecordValidation] = []

    for record in records:
        issues: list[RecordIssueEvidence] = []

        for field_name, value in (
            ("schema_version", record.schema_version),
            ("pricing.currency", record.pricing.currency),
            ("provenance.adapter", record.provenance.adapter),
            ("provenance.configuration_version", record.provenance.configuration_version),
            ("provenance.rule_version", record.provenance.rule_version),
        ):
            if not value.strip():
                issues.append(
                    RecordIssueEvidence(
                        rule_id="FR-004",
                        issue_code=ISSUE_MISSING_REQUIRED_FIELD,
                        severity=SEVERITY_ERROR,
                        classification=CLASSIFICATION_VALIDATION,
                        evidence={"field": field_name},
                    )
                )

        if record.key in seen:
            issues.append(
                RecordIssueEvidence(
                    rule_id="BR-018",
                    issue_code=ISSUE_DUPLICATE_CANONICAL_KEY,
                    severity=SEVERITY_ERROR,
                    classification=CLASSIFICATION_VALIDATION,
                    evidence={
                        "store_code": record.key.store_code,
                        "item_code": record.key.item_code,
                        "selling_uom": record.key.selling_uom,
                    },
                )
            )
        seen.add(record.key)

        results.append(
            RecordValidation(
                key=record.key,
                validation_status=(
                    ValidationStatus.REJECTED if issues else ValidationStatus.VALID
                ),
                issues=tuple(issues),
            )
        )

    return tuple(results)


def promotion_issues(
    evaluation: PromotionEvaluationEvidence,
) -> tuple[RecordIssueEvidence, ...]:
    """Surface an evaluation's reason and fallback codes as record issues.

    Nothing is recomputed here: the codes come from the rules in
    :mod:`esl_service.domain.promotion_rules`. Ambiguity is reported without
    choosing a winner, which remains #37's decision.
    """

    issues: list[RecordIssueEvidence] = []

    if evaluation.outcome is PromotionOutcome.AMBIGUOUS:
        issues.append(
            RecordIssueEvidence(
                rule_id="BR-019",
                issue_code=ISSUE_PROMOTION_AMBIGUOUS,
                severity=SEVERITY_ERROR,
                classification=CLASSIFICATION_PROMOTION,
                evidence={
                    "eligible_campaigns": [
                        item.source_campaign_id
                        for item in evaluation.candidates
                        if item.eligibility is CandidateEligibility.ELIGIBLE
                    ]
                },
            )
        )

    for candidate in evaluation.candidates:
        for code in candidate.reason_codes:
            issues.append(_promotion_issue(code, candidate.source_campaign_id, SEVERITY_ERROR))
        for code in candidate.fallback_codes:
            issues.append(
                _promotion_issue(code, candidate.source_campaign_id, SEVERITY_WARNING)
            )

    return tuple(issues)


def assess_record(
    *,
    validation: RecordValidation,
    evaluation: PromotionEvaluationEvidence | None,
    current_page: int | None,
    desired_page: int | None,
) -> RecordProcessingEvidence:
    """Combine validation and promotion evidence into one record outcome.

    A rejected or unresolved record never requests an external action, so
    quarantined work cannot cause an effect (FR-003, FR-006). A record whose
    current page already equals its desired page is idempotently skipped
    (BR-008).
    """

    issues = list(validation.issues)
    if evaluation is not None:
        issues.extend(promotion_issues(evaluation))

    if validation.validation_status is ValidationStatus.REJECTED:
        return RecordProcessingEvidence(
            key=validation.key,
            validation_status=ValidationStatus.REJECTED,
            eligibility_status=EligibilityStatus.INELIGIBLE,
            promotion_outcome=None if evaluation is None else evaluation.outcome,
            current_page=current_page,
            desired_page=desired_page,
            action_decision=ActionDecision.NONE,
            processing_status=ProcessingStatus.REJECTED,
            issues=tuple(issues),
        )

    outcome = None if evaluation is None else evaluation.outcome
    if outcome in (PromotionOutcome.UNRESOLVED, PromotionOutcome.AMBIGUOUS):
        eligibility = EligibilityStatus.UNRESOLVED
        status = ProcessingStatus.UNRESOLVED
        action = ActionDecision.NONE
    elif outcome is PromotionOutcome.REJECTED:
        eligibility = EligibilityStatus.INELIGIBLE
        status = ProcessingStatus.INELIGIBLE
        action = ActionDecision.NONE
    elif desired_page is None or current_page == desired_page:
        eligibility = EligibilityStatus.ELIGIBLE
        status = ProcessingStatus.UNCHANGED
        action = ActionDecision.SKIP_IDEMPOTENT
    else:
        eligibility = EligibilityStatus.ELIGIBLE
        status = ProcessingStatus.ACTION_REQUIRED
        action = ActionDecision.PAGE_CHANGE

    return RecordProcessingEvidence(
        key=validation.key,
        validation_status=ValidationStatus.VALID,
        eligibility_status=eligibility,
        promotion_outcome=outcome,
        current_page=current_page,
        desired_page=desired_page,
        action_decision=action,
        processing_status=status,
        issues=tuple(issues),
    )


def _promotion_issue(
    code: str, campaign_id: str, severity: str
) -> RecordIssueEvidence:
    """Build one issue for a promotion reason or fallback code."""

    return RecordIssueEvidence(
        rule_id=_PROMOTION_RULE_BY_CODE.get(code, "BR-005"),
        issue_code=code,
        severity=severity,
        classification=CLASSIFICATION_PROMOTION,
        evidence={"source_campaign_id": campaign_id},
    )
