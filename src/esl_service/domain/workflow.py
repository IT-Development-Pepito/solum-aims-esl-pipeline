"""Explicit, transport-independent workflow semantics for FR-007."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Approved execution states from the authoritative architecture."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_EXCEPTIONS = "SUCCEEDED_WITH_EXCEPTIONS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.SKIPPED,
    }
)

_ALLOWED_EXECUTION_TRANSITIONS: Mapping[
    ExecutionStatus, frozenset[ExecutionStatus]
] = {
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.SKIPPED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.RETRY_WAIT,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.RETRY_WAIT: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.RECOVERING: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.FAILED}
    ),
}


@dataclass(frozen=True)
class WorkflowTransitionEvent:
    """Structured evidence that persistence can append to the execution audit trail."""

    event_type: str
    previous_status: ExecutionStatus
    requested_status: ExecutionStatus
    reason_code: str

    @property
    def payload(self) -> dict[str, str]:
        """Return the stable, secret-free event payload."""

        return {
            "from_status": self.previous_status.value,
            "to_status": self.requested_status.value,
            "reason_code": self.reason_code,
        }


class InvalidWorkflowTransition(ValueError):
    """Reject an invalid state change while retaining auditable evidence."""

    def __init__(self, audit_event: WorkflowTransitionEvent) -> None:
        self.audit_event = audit_event
        super().__init__(
            "invalid execution transition: "
            f"{audit_event.previous_status.value} -> "
            f"{audit_event.requested_status.value}"
        )


def transition_execution(
    previous_status: ExecutionStatus,
    requested_status: ExecutionStatus,
) -> WorkflowTransitionEvent:
    """Validate one execution transition and return structured audit evidence."""

    if requested_status not in _ALLOWED_EXECUTION_TRANSITIONS.get(
        previous_status, frozenset()
    ):
        raise InvalidWorkflowTransition(
            WorkflowTransitionEvent(
                event_type="WORKFLOW_TRANSITION_REJECTED",
                previous_status=previous_status,
                requested_status=requested_status,
                reason_code="INVALID_EXECUTION_TRANSITION",
            )
        )
    return WorkflowTransitionEvent(
        event_type="WORKFLOW_TRANSITION_ACCEPTED",
        previous_status=previous_status,
        requested_status=requested_status,
        reason_code="EXECUTION_TRANSITION_ACCEPTED",
    )


def is_terminal(status: ExecutionStatus) -> bool:
    """Return whether an execution status permits no further transition."""

    return status in _TERMINAL_EXECUTION_STATUSES


class StepOutcome(StrEnum):
    """Observable states used by dependency conditions."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_EXCEPTIONS = "SUCCEEDED_WITH_EXCEPTIONS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


_TERMINAL_STEP_OUTCOMES = frozenset(
    {
        StepOutcome.SUCCEEDED,
        StepOutcome.SUCCEEDED_WITH_EXCEPTIONS,
        StepOutcome.FAILED,
        StepOutcome.CANCELLED,
        StepOutcome.SKIPPED,
    }
)


@dataclass(frozen=True)
class StepDependency:
    """A predecessor and the terminal outcomes that satisfy its condition."""

    step_name: str
    accepted_outcomes: frozenset[StepOutcome]

    def __post_init__(self) -> None:
        if not self.step_name:
            raise ValueError("dependency step name must not be empty")
        if not self.accepted_outcomes:
            raise ValueError("dependency must accept at least one terminal outcome")
        if not self.accepted_outcomes <= _TERMINAL_STEP_OUTCOMES:
            raise ValueError("dependency conditions require terminal outcomes")


@dataclass(frozen=True)
class WorkflowStep:
    """One named workflow step with explicit predecessor conditions."""

    name: str
    dependencies: tuple[StepDependency, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("workflow step name must not be empty")
        dependency_names = tuple(item.step_name for item in self.dependencies)
        if len(dependency_names) != len(set(dependency_names)):
            raise ValueError(f"duplicate dependency for workflow step {self.name!r}")


@dataclass(frozen=True)
class WorkflowDefinition:
    """Validated workflow graph with deterministic dependency ordering."""

    steps: tuple[WorkflowStep, ...]
    _ordered_step_names: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        step_by_name = {step.name: step for step in self.steps}
        if len(step_by_name) != len(self.steps):
            raise ValueError("workflow step names must be unique")

        for step in self.steps:
            for dependency in step.dependencies:
                if dependency.step_name not in step_by_name:
                    raise ValueError(
                        f"unknown dependency {dependency.step_name!r} "
                        f"for workflow step {step.name!r}"
                    )

        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_name: str) -> None:
            if step_name in visiting:
                raise ValueError(f"cyclic dependency involving {step_name!r}")
            if step_name in visited:
                return
            visiting.add(step_name)
            for dependency in step_by_name[step_name].dependencies:
                visit(dependency.step_name)
            visiting.remove(step_name)
            visited.add(step_name)
            ordered.append(step_name)

        for step in self.steps:
            visit(step.name)

        object.__setattr__(self, "_ordered_step_names", tuple(ordered))

    @property
    def ordered_step_names(self) -> tuple[str, ...]:
        """Return stable dependency-first ordering."""

        return self._ordered_step_names

    def ready_step_names(
        self, outcomes: Mapping[str, StepOutcome]
    ) -> tuple[str, ...]:
        """Return incomplete steps whose explicit dependency conditions are met."""

        known_names = set(self._ordered_step_names)
        unknown_names = set(outcomes) - known_names
        if unknown_names:
            raise ValueError(f"outcomes contain unknown workflow steps: {unknown_names}")

        step_by_name = {step.name: step for step in self.steps}
        ready: list[str] = []
        for step_name in self._ordered_step_names:
            if step_name in outcomes:
                continue
            dependencies = step_by_name[step_name].dependencies
            if all(
                outcomes.get(dependency.step_name) in dependency.accepted_outcomes
                for dependency in dependencies
            ):
                ready.append(step_name)
        return tuple(ready)
