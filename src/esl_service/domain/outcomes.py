"""Execution input scope and outcome vocabulary for restart-safe processing.

Execution states and their transition graph live in
:mod:`esl_service.domain.workflow` and are not redefined here. This module adds
only the immutable input scope an execution is created from (FR-002, FR-010,
FR-025) and the controlled vocabularies persistence stores alongside it.

``FailureClass`` is the vocabulary required by FR-014. The policy that decides
which class a failure belongs to is out of scope here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.domain.serialization import JSONValue, sanitize_evidence


class ExecutionMode(StrEnum):
    """Whether an execution may cause an external effect."""

    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class TriggerType(StrEnum):
    """What caused an execution to be created."""

    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    RETRY = "RETRY"
    REPLAY = "REPLAY"
    RECOVERY = "RECOVERY"


class FailureClass(StrEnum):
    """FR-014 failure classification vocabulary."""

    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"


@dataclass(frozen=True)
class NewExecution:
    """The complete, reproducible scope one execution is created from.

    Every execution references exactly one configuration version and one rule
    version, so a run can be reproduced and audited later (FR-002, FR-025).
    Retry and replay link to their origin rather than overwriting it (FR-011).
    """

    workflow_name: str
    store_code: str
    trigger_type: TriggerType
    mode: ExecutionMode
    correlation_id: UUID
    source_window_start: datetime
    source_window_end: datetime
    configuration_version_id: UUID
    rule_version: str
    requested_by: str | None = None
    reason: str | None = None
    retry_of_execution_id: UUID | None = None
    replay_of_execution_id: UUID | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("workflow_name", self.workflow_name),
            ("store_code", self.store_code),
            ("rule_version", self.rule_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")

        for name, moment in (
            ("source_window_start", self.source_window_start),
            ("source_window_end", self.source_window_end),
        ):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")

        if self.source_window_start > self.source_window_end:
            raise ValueError("source_window_start must not follow source_window_end")


class ValidationStatus(StrEnum):
    """Whether a source record passed structural validation (FR-003)."""

    VALID = "VALID"
    REJECTED = "REJECTED"


class EligibilityStatus(StrEnum):
    """Whether a validated record can be acted on (FR-006)."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNRESOLVED = "UNRESOLVED"


class ActionDecision(StrEnum):
    """The external action a record calls for, if any (BR-007, BR-008)."""

    NONE = "NONE"
    PAGE_CHANGE = "PAGE_CHANGE"
    SKIP_IDEMPOTENT = "SKIP_IDEMPOTENT"


class ProcessingStatus(StrEnum):
    """The terminal category one record reached in one execution."""

    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"
    INELIGIBLE = "INELIGIBLE"
    UNCHANGED = "UNCHANGED"
    ACTION_REQUIRED = "ACTION_REQUIRED"


@dataclass(frozen=True)
class RecordIssueEvidence:
    """One independently queryable reason a record was not processed cleanly.

    Each issue names the rule it came from and a stable code, so rejections
    and anomalies stay deterministic and can be counted and replayed after a
    correction (FR-003, FR-006, FR-022).
    """

    rule_id: str
    issue_code: str
    severity: str
    classification: str
    evidence: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        for name, value in (
            ("rule_id", self.rule_id),
            ("issue_code", self.issue_code),
            ("severity", self.severity),
            ("classification", self.classification),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        # Raises rather than redacting, so a leaking caller is fixed at source.
        sanitize_evidence(dict(self.evidence))


@dataclass(frozen=True)
class RecordProcessingEvidence:
    """What happened to one canonical record in one execution.

    A record that failed validation or is unresolved never carries an action
    decision, so quarantined work cannot reach an external effect.
    """

    key: CanonicalKey
    validation_status: ValidationStatus
    eligibility_status: EligibilityStatus
    promotion_outcome: PromotionOutcome | None
    current_page: int | None
    desired_page: int | None
    action_decision: ActionDecision
    processing_status: ProcessingStatus
    issues: tuple[RecordIssueEvidence, ...]

    def __post_init__(self) -> None:
        blocked = (
            self.validation_status is ValidationStatus.REJECTED
            or self.eligibility_status is EligibilityStatus.UNRESOLVED
        )
        if blocked and self.action_decision is not ActionDecision.NONE:
            raise ValueError(
                "a rejected record or unresolved record requests no action"
            )
        if (
            self.validation_status is ValidationStatus.REJECTED
            or self.processing_status
            in (ProcessingStatus.REJECTED, ProcessingStatus.UNRESOLVED)
        ) and not self.issues:
            raise ValueError(
                "a rejected or unresolved record must carry at least one issue"
            )
