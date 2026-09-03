"""The recovery report read model (#21, FR-016, NFR-002).

After an interruption an operator needs four things: what scope the run
covers, where a restart resumes from, which external effects are uncertain,
and what to do next. All four are derived here from state that already
exists, the execution row, its step history with checkpoints, and the
actions whose external outcome is unknown, so the report can never disagree
with the evidence it summarises. Nothing is stored; presentation through
``esl-admin``, the API, and metrics belongs to #109.

The next operator action follows the architecture's failure table (section
8) and the ``WORKFLOW.md`` failure scenarios: an unknown external outcome is
reconciled before anything else and never resent automatically; a live,
recovering, or waiting run needs no operator and must not be duplicated; an
exhausted retryable failure is restored then retried; a non-retryable one is
corrected then replayed; an unclassified one is reviewed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from esl_service.application.runner import RUN_STEPS
from esl_service.domain.outcomes import FailureClass
from esl_service.domain.workflow import ExecutionStatus, StepOutcome

#: The operator action of a run that needs none.
NO_ACTION = "None."


class RecoveryExecution(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def workflow_name(self) -> str: ...

    @property
    def store_code(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def terminal_reason(self) -> str | None: ...

    @property
    def retry_not_before(self) -> datetime | None: ...

    @property
    def source_window_start(self) -> datetime: ...

    @property
    def source_window_end(self) -> datetime: ...


class RecoveryCheckpoint(Protocol):
    @property
    def checkpoint_key(self) -> str: ...

    @property
    def watermark(self) -> str: ...


class RecoveryStep(Protocol):
    """One step's latest attempt, as ``step_history`` returns it."""

    @property
    def step_name(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def outcome(self) -> str: ...

    @property
    def checkpoints(self) -> Sequence[RecoveryCheckpoint]: ...


@dataclass(frozen=True)
class UncertainAction:
    """An action whose external effect is unknown; it blocks completion (FR-013)."""

    action_id: UUID
    idempotency_key: str
    state: str


@dataclass(frozen=True)
class RecoveryReport:
    execution_id: UUID
    scope: str
    source_window_start: datetime
    source_window_end: datetime
    status: str
    terminal_reason: str | None
    #: ``<step>:<checkpoint_key> @ <watermark>`` of the last succeeded step, or None.
    checkpoint: str | None
    #: The first step a resumed run repeats.
    resume_from: str | None
    external_uncertainty: tuple[UncertainAction, ...]
    next_operator_action: str


class RecoveryExecutionPort(Protocol):
    """The ``ExecutionRepository`` methods the report reads."""

    def get_execution(self, execution_id: UUID) -> RecoveryExecution: ...

    def step_history(self, execution_id: UUID) -> Sequence[RecoveryStep]: ...


class RecoveryActionRow(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def idempotency_key(self) -> str: ...

    @property
    def state(self) -> str: ...


class RecoveryActionPort(Protocol):
    """The ``ActionRepository`` method the report reads."""

    def unresolved_actions(self, *, execution_id: UUID | None = None) -> Sequence[RecoveryActionRow]: ...


def report_for(
    execution_id: UUID,
    *,
    executions: RecoveryExecutionPort,
    actions: RecoveryActionPort,
    now: datetime,
) -> RecoveryReport:
    """Assemble the report of one execution from the state store's repositories."""

    return recovery_report(
        executions.get_execution(execution_id),
        executions.step_history(execution_id),
        [
            UncertainAction(action.id, action.idempotency_key, action.state)
            for action in actions.unresolved_actions(execution_id=execution_id)
        ],
        now=now,
    )


def recovery_report(
    execution: RecoveryExecution,
    steps: Sequence[RecoveryStep],
    uncertain_actions: Sequence[UncertainAction],
    *,
    now: datetime,
) -> RecoveryReport:
    """Derive the four recovery fields of one execution from its durable state."""

    del now  # reserved for the report's own timestamp; the fields are clock-free
    latest = {step.step_name: step for step in steps}
    checkpoint = _last_checkpoint(latest)
    resume_from = _resume_from(latest)
    uncertainty = tuple(uncertain_actions)
    action = _next_operator_action(execution, checkpoint, resume_from, uncertainty)
    return RecoveryReport(
        execution_id=execution.id,
        scope=f"{execution.workflow_name}:{execution.store_code}",
        source_window_start=execution.source_window_start,
        source_window_end=execution.source_window_end,
        status=execution.status,
        terminal_reason=execution.terminal_reason,
        checkpoint=checkpoint,
        resume_from=resume_from,
        external_uncertainty=uncertainty,
        next_operator_action=action,
    )


def _last_checkpoint(latest: dict[str, RecoveryStep]) -> str | None:
    """The last checkpoint of the last succeeded step in the procedure's order."""

    for step_name in reversed(RUN_STEPS):
        step = latest.get(step_name)
        if step is None or step.outcome != StepOutcome.SUCCEEDED.value or not step.checkpoints:
            continue
        last = step.checkpoints[-1]
        return f"{last.checkpoint_key} @ {last.watermark}"
    return None


def _resume_from(latest: dict[str, RecoveryStep]) -> str | None:
    """The first step whose latest attempt did not succeed; None when every step did."""

    for step_name in RUN_STEPS:
        step = latest.get(step_name)
        if step is None or step.outcome != StepOutcome.SUCCEEDED.value:
            return step_name
    return None


def _next_operator_action(
    execution: RecoveryExecution,
    checkpoint: str | None,
    resume_from: str | None,
    uncertainty: tuple[UncertainAction, ...],
) -> str:
    if uncertainty:
        return (
            f"Reconcile {len(uncertainty)} action(s) whose external outcome is unknown before "
            "any resend; they are never resubmitted automatically."
        )
    status = ExecutionStatus(execution.status)
    if status is ExecutionStatus.RETRY_WAIT:
        # Rendered in UTC so the text does not depend on the reading session's zone.
        due = (
            execution.retry_not_before.astimezone(UTC).isoformat()
            if execution.retry_not_before
            else "the policy delay"
        )
        return (
            f"None: the retry is due at {due} and resumes from {resume_from}; "
            "do not launch a duplicate manual run."
        )
    if status is ExecutionStatus.RECOVERING:
        after = f" after checkpoint {checkpoint}" if checkpoint else ""
        return (
            f"None: startup recovery resumes from {resume_from}{after}; "
            "do not launch a duplicate manual run."
        )
    if status is ExecutionStatus.RUNNING:
        return "None: a live process owns the run; if that process is gone, startup recovery marks it RECOVERING."
    if status is ExecutionStatus.QUEUED:
        return "None: the worker picks the run when it is due."
    if status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS:
        return "Review the reconciliation report's exceptions; the run itself is complete."
    if status is ExecutionStatus.FAILED:
        return _action_for_terminal_reason(execution.terminal_reason, resume_from)
    return NO_ACTION


def _action_for_terminal_reason(reason: str | None, resume_from: str | None) -> str:
    """Read ``step:dependency:kind:class[:attempts_exhausted]`` as the runner writes it."""

    parts = (reason or "").split(":")
    if len(parts) < 4:
        return f"Review terminal reason {reason!r}; nothing is retried automatically."
    step, dependency, kind = parts[0], parts[1], parts[2]
    failure_class = parts[3]
    if failure_class == FailureClass.RETRYABLE.value:
        return f"Restore {dependency} ({kind}), then retry the run; it resumes from {resume_from or step}."
    if failure_class == FailureClass.NON_RETRYABLE.value:
        return (
            f"Correct {dependency} ({kind}) through the approved process, then replay the bounded "
            "window; nothing is retried automatically."
        )
    return f"Review {step}: {reason}; nothing is retried automatically."


__all__ = [
    "NO_ACTION",
    "RecoveryActionPort",
    "RecoveryActionRow",
    "RecoveryCheckpoint",
    "RecoveryExecution",
    "RecoveryExecutionPort",
    "RecoveryReport",
    "RecoveryStep",
    "UncertainAction",
    "recovery_report",
    "report_for",
]
