"""Sanitized audit read models (FR-022, NFR-007, NFR-009).

These are the operator-facing shapes. They expose identifiers, versions,
counts, and summaries, and never an unrestricted JSONB column, because those
carry evidence payloads that were only ever sanitized for internal storage.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from esl_service.web.audit_schemas import (
    ExecutionAuditResponse,
    ExecutionEventSummary,
    ReconciliationExceptionResponse,
    RecordEvidenceResponse,
    RecordIssueSummary,
    RunIssueDetailResponse,
    StepEvidenceResponse,
)

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def execution_payload(**overrides: object) -> dict[str, object]:
    """Build a complete execution audit payload."""

    values: dict[str, object] = {
        "execution_id": uuid4(),
        "workflow_name": "sku-shadow",
        "store_code": "084",
        "mode": "SHADOW",
        "trigger_type": "SCHEDULED",
        "status": "SUCCEEDED",
        "correlation_id": uuid4(),
        "configuration_version_id": uuid4(),
        "rule_version": "rules-v1",
        "source_window_start": NOW,
        "source_window_end": NOW,
        "started_at": NOW,
        "ended_at": NOW,
        "terminal_reason": None,
        "requested_by": "operator@example",
        "reason": "INC-1",
        "events": [
            ExecutionEventSummary(
                sequence=1, event_type="WORKFLOW_TRANSITION_ACCEPTED", occurred_at=NOW
            )
        ],
        "counts": {"extracted": 2, "unresolved": 1},
    }
    values.update(overrides)
    return values


def record_payload(**overrides: object) -> dict[str, object]:
    """Build a complete record evidence payload."""

    values: dict[str, object] = {
        "store_code": "084",
        "item_code": "101024011793",
        "selling_uom": "KGS",
        "canonical_hash": "a" * 64,
        "validation_status": "VALID",
        "eligibility_status": "UNRESOLVED",
        "promotion_outcome": "UNRESOLVED",
        "processing_status": "UNRESOLVED",
        "current_page": 1,
        "desired_page": 2,
        "action_decision": "NONE",
        "issues": [
            RecordIssueSummary(
                sequence=0,
                rule_id="BR-013",
                issue_code="UOM_RULE_REQUIRED",
                severity="ERROR",
                classification="PROMOTION",
            )
        ],
        "candidate_campaign_ids": ["A", "B"],
        "action_states": ["INTENDED"],
    }
    values.update(overrides)
    return values


# --- no internal payload reaches an operator --------------------------------


def test_execution_response_excludes_internal_payload() -> None:
    """Event payload JSONB is internal evidence, not an operator field."""

    response = ExecutionAuditResponse.model_validate(execution_payload())
    dumped = response.model_dump()

    assert "payload" not in dumped
    assert "payload" not in str(dumped["events"])


def test_record_response_excludes_raw_evidence() -> None:
    """Issue evidence and canonical payload JSONB are never exposed."""

    dumped = RecordEvidenceResponse.model_validate(record_payload()).model_dump()

    assert "evidence" not in str(dumped)
    assert "payload" not in dumped


@pytest.mark.parametrize(
    "secret", ["password", "token", "authorization", "database_url", "dpapi"]
)
def test_responses_contain_no_secret_like_key(secret: str) -> None:
    """Neither read model carries a secret-like field name (NFR-009)."""

    execution = ExecutionAuditResponse.model_validate(execution_payload())
    record = RecordEvidenceResponse.model_validate(record_payload())

    assert secret not in execution.model_dump_json().lower()
    assert secret not in record.model_dump_json().lower()


def test_unknown_field_is_rejected_rather_than_passed_through() -> None:
    """A response model must not silently carry an unmodelled column."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExecutionAuditResponse.model_validate(
            execution_payload(secret_bundle_path="C:/ProgramData/secrets.dpapi")
        )


def test_run_issue_response_rejects_extra_fields_and_secret_evidence() -> None:
    """A JSONB column or credential-shaped evidence key must fail closed."""

    from pydantic import ValidationError

    values = {
        "store_code": "084",
        "item_code": "A",
        "selling_uom": "KGS",
        "rule_id": "BR-006",
        "issue_code": "MISSING_PRICE",
        "severity": "ERROR",
        "evidence": {"price_category": "001"},
        "keyless": False,
    }
    with pytest.raises(ValidationError):
        RunIssueDetailResponse.model_validate({**values, "raw_payload": {"x": 1}})
    with pytest.raises(ValidationError, match="forbidden evidence key"):
        RunIssueDetailResponse.model_validate({**values, "evidence": {"api_token": "needle"}})


def test_reconciliation_and_step_responses_reject_unmodelled_payloads() -> None:
    """Neither exception nor checkpoint JSONB may pass through by accident."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="forbidden evidence key"):
        ReconciliationExceptionResponse.model_validate(
            {
                "sequence": 1,
                "category": "LEGACY_BASELINE_MISMATCH",
                "store_code": "084",
                "item_code": "A",
                "selling_uom": "KGS",
                "expected_evidence": {"database_url": "needle"},
                "actual_evidence": None,
                "resolution_status": "OPEN",
            }
        )
    with pytest.raises(ValidationError):
        StepEvidenceResponse.model_validate(
            {
                "step_name": "canonicalize",
                "attempt": 1,
                "outcome": "SUCCEEDED",
                "failure_class": None,
                "started_at": NOW,
                "ended_at": NOW,
                "duration_seconds": 0.0,
                "checkpoint_key": "canonicalize:done",
                "checkpoint_watermark": "wm",
                "checkpoint_counts": {"records": 1},
                "payload": {"must": "not pass"},
            }
        )


# --- the audit answers the FR-022 questions ---------------------------------


def test_execution_response_answers_who_what_when_and_why() -> None:
    """One response answers actor, scope, timing, configuration, and outcome."""

    response = ExecutionAuditResponse.model_validate(execution_payload())

    assert response.requested_by == "operator@example"
    assert response.reason == "INC-1"
    assert response.workflow_name == "sku-shadow"
    assert response.store_code == "084"
    assert response.rule_version == "rules-v1"
    assert response.configuration_version_id is not None
    assert response.status == "SUCCEEDED"
    assert response.counts["unresolved"] == 1


def test_record_response_carries_issue_codes_and_candidates() -> None:
    """Promotion evidence is summarised by code and campaign, not raw JSON."""

    response = RecordEvidenceResponse.model_validate(record_payload())

    assert [item.issue_code for item in response.issues] == ["UOM_RULE_REQUIRED"]
    assert response.candidate_campaign_ids == ("A", "B")
    assert response.action_states == ("INTENDED",)


def test_event_summary_keeps_order_and_type_only() -> None:
    """An event summary is enough to trace a run without exposing evidence."""

    summary = ExecutionEventSummary(
        sequence=7, event_type="ACTION_DUPLICATE_DETECTED", occurred_at=NOW
    )
    assert set(summary.model_dump()) == {"sequence", "event_type", "occurred_at"}
