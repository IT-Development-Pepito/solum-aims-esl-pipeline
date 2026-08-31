"""Execution input scope and outcome vocabulary for restart-safe processing.

Execution states and their transition graph live in
:mod:`esl_service.domain.workflow` and are not redefined here. This module adds
only the immutable input scope an execution is created from (FR-002, FR-010,
FR-025) and the controlled vocabularies persistence stores alongside it.

``FailureClass`` is the vocabulary required by FR-014. The policy that decides
which class a failure belongs to is out of scope here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


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
