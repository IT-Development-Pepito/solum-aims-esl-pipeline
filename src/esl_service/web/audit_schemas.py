"""Sanitized operator-facing audit read models (FR-022, NFR-007, NFR-009).

These shapes answer the audit questions — who, what, when, why, with which
configuration and input, to what outcome — using identifiers, versions, counts,
and summaries.

No model exposes a JSONB column. Event payloads, issue evidence, canonical
record payloads, and adapter responses were sanitized for *internal* storage;
that is not the same as being safe to hand to an operator interface, so they
are summarised by code and type instead. ``extra="forbid"`` means a new column
cannot reach a response by being added upstream.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Sanitized(BaseModel):
    """Base rejecting any field the model does not explicitly declare."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionEventSummary(_Sanitized):
    """One structured event, without its payload."""

    sequence: int
    event_type: str
    occurred_at: datetime


class RecordIssueSummary(_Sanitized):
    """One record issue, by rule and code rather than raw evidence."""

    sequence: int
    rule_id: str
    issue_code: str
    severity: str
    classification: str


class ExecutionAuditResponse(_Sanitized):
    """One execution's audit answer for an authorized operator."""

    execution_id: UUID
    workflow_name: str
    store_code: str
    mode: str
    trigger_type: str
    status: str
    correlation_id: UUID
    configuration_version_id: UUID | None
    rule_version: str
    source_window_start: datetime
    source_window_end: datetime
    started_at: datetime
    ended_at: datetime | None
    terminal_reason: str | None
    requested_by: str | None
    reason: str | None
    events: tuple[ExecutionEventSummary, ...]
    counts: dict[str, int]


class RecordEvidenceResponse(_Sanitized):
    """One record's outcome and the evidence summaries behind it."""

    store_code: str
    item_code: str
    selling_uom: str
    canonical_hash: str
    validation_status: str
    eligibility_status: str
    promotion_outcome: str | None
    processing_status: str
    current_page: int | None
    desired_page: int | None
    action_decision: str
    issues: tuple[RecordIssueSummary, ...]
    candidate_campaign_ids: tuple[str, ...]
    action_states: tuple[str, ...]
