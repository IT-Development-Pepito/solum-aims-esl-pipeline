"""Record outcome and issue contracts (FR-003, FR-006, NFR-009).

A record that cannot be processed is quarantined with deterministic reasons
rather than silently dropped, and issue evidence may never carry a secret.
"""

import pytest

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ProcessingStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.domain.serialization import sanitize_evidence

KEY = CanonicalKey("084", "101024011793", "KGS")


def issue(
    rule_id: str = "FR-003",
    issue_code: str = "MISSING_REQUIRED_FIELD",
    **overrides: object,
) -> RecordIssueEvidence:
    """Build one issue record."""

    values: dict[str, object] = {
        "rule_id": rule_id,
        "issue_code": issue_code,
        "severity": "ERROR",
        "classification": "VALIDATION",
        "evidence": {"field": "pricing.currency"},
    }
    values.update(overrides)
    return RecordIssueEvidence(**values)  # type: ignore[arg-type]


def processing(**overrides: object) -> RecordProcessingEvidence:
    """Build one record processing outcome."""

    values: dict[str, object] = {
        "key": KEY,
        "validation_status": ValidationStatus.VALID,
        "eligibility_status": EligibilityStatus.ELIGIBLE,
        "promotion_outcome": PromotionOutcome.SELECTED,
        "current_page": 1,
        "desired_page": 2,
        "action_decision": ActionDecision.PAGE_CHANGE,
        "processing_status": ProcessingStatus.ACTION_REQUIRED,
        "issues": (),
    }
    values.update(overrides)
    return RecordProcessingEvidence(**values)  # type: ignore[arg-type]


# --- secret-safe evidence (NFR-009) ----------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "connection_string",
        "database_url",
        "dpapi",
        "DATABASE_URL",
        "api_token_value",
    ],
)
def test_issue_evidence_rejects_secret_like_keys(key: str) -> None:
    """Evidence must never carry a credential, whatever its casing."""

    with pytest.raises(ValueError, match="forbidden evidence key"):
        issue(evidence={key: "value"})


def test_secret_like_keys_are_rejected_when_nested() -> None:
    """The check is recursive, so nesting cannot smuggle a secret through."""

    with pytest.raises(ValueError, match="forbidden evidence key"):
        sanitize_evidence({"outer": {"inner": [{"db_password": "x"}]}})


def test_ordinary_evidence_survives_sanitisation() -> None:
    """Legitimate diagnostic evidence is preserved exactly."""

    payload = {"field": "pricing.currency", "observed": None, "candidates": [1, 2]}
    assert sanitize_evidence(payload) == payload


# --- outcome invariants -----------------------------------------------------


def test_rejected_validation_cannot_require_an_action() -> None:
    """A rejected record must not reach an external action (FR-003)."""

    with pytest.raises(ValueError, match="rejected record"):
        processing(
            validation_status=ValidationStatus.REJECTED,
            processing_status=ProcessingStatus.ACTION_REQUIRED,
            issues=(issue(),),
        )


def test_rejected_validation_requires_a_reason() -> None:
    """A rejection is only traceable when it carries at least one issue."""

    with pytest.raises(ValueError, match="at least one issue"):
        processing(
            validation_status=ValidationStatus.REJECTED,
            eligibility_status=EligibilityStatus.INELIGIBLE,
            action_decision=ActionDecision.NONE,
            processing_status=ProcessingStatus.REJECTED,
            issues=(),
        )


def test_unresolved_outcome_requires_a_reason() -> None:
    """An unresolved record must say what is unresolved (FR-006)."""

    with pytest.raises(ValueError, match="at least one issue"):
        processing(
            eligibility_status=EligibilityStatus.UNRESOLVED,
            action_decision=ActionDecision.NONE,
            processing_status=ProcessingStatus.UNRESOLVED,
            promotion_outcome=PromotionOutcome.UNRESOLVED,
            issues=(),
        )


def test_record_retains_multiple_independent_issues() -> None:
    """Several issues are queryable independently rather than concatenated."""

    result = processing(
        eligibility_status=EligibilityStatus.UNRESOLVED,
        promotion_outcome=PromotionOutcome.UNRESOLVED,
        processing_status=ProcessingStatus.UNRESOLVED,
        action_decision=ActionDecision.NONE,
        issues=(
            issue("BR-013", "UOM_RULE_REQUIRED", classification="PROMOTION"),
            issue("BR-006", "CATEGORY_001_PRICE_MISSING", classification="PROMOTION"),
        ),
    )
    assert [item.issue_code for item in result.issues] == [
        "UOM_RULE_REQUIRED",
        "CATEGORY_001_PRICE_MISSING",
    ]


def test_valid_unchanged_record_needs_no_issue() -> None:
    """A record that is simply unchanged is not an anomaly."""

    result = processing(
        promotion_outcome=PromotionOutcome.NO_PROMOTION,
        current_page=1,
        desired_page=1,
        action_decision=ActionDecision.SKIP_IDEMPOTENT,
        processing_status=ProcessingStatus.UNCHANGED,
    )
    assert result.issues == ()


def test_issue_requires_a_rule_and_code() -> None:
    """Every issue is traceable to a rule identifier and a stable code."""

    with pytest.raises(ValueError, match="rule_id"):
        issue(rule_id="  ")
    with pytest.raises(ValueError, match="issue_code"):
        issue(issue_code="")
