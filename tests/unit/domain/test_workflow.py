"""Requirement-traceable tests for explicit workflow semantics (FR-007)."""

import importlib
from collections.abc import Iterable
from itertools import pairwise

import pytest


def _workflow_module():  # type: ignore[no-untyped-def]
    """Load the wished-for contract inside tests so the red phase is observable."""

    return importlib.import_module("esl_service.domain.workflow")


@pytest.mark.parametrize(
    ("states", "terminal_status"),
    [
        (("QUEUED", "RUNNING", "SUCCEEDED"), "SUCCEEDED"),
        (("QUEUED", "SKIPPED"), "SKIPPED"),
        (
            ("QUEUED", "RUNNING", "RETRY_WAIT", "RUNNING", "SUCCEEDED"),
            "SUCCEEDED",
        ),
        (("QUEUED", "RUNNING", "FAILED"), "FAILED"),
        (("QUEUED", "CANCELLED"), "CANCELLED"),
        (
            ("QUEUED", "RUNNING", "RECOVERING", "RUNNING", "SUCCEEDED"),
            "SUCCEEDED",
        ),
    ],
    ids=("success", "skipped", "retrying", "failed", "cancelled", "recovered"),
)
def test_fr_007_execution_lifecycle_is_explicit(
    states: tuple[str, ...], terminal_status: str
) -> None:
    """Removing an approved lifecycle edge must fail an FR-007 scenario."""

    workflow = _workflow_module()
    statuses = [workflow.ExecutionStatus(value) for value in states]

    transitions = [
        workflow.transition_execution(previous, requested)
        for previous, requested in pairwise(statuses)
    ]

    assert [transition.event_type for transition in transitions] == [
        "WORKFLOW_TRANSITION_ACCEPTED"
    ] * len(transitions)
    assert transitions[-1].requested_status.value == terminal_status
    assert workflow.is_terminal(statuses[-1]) is True


def test_fr_007_non_terminal_status_is_not_terminal() -> None:
    """Misclassifying active work as terminal must be visible."""

    workflow = _workflow_module()

    assert workflow.is_terminal(workflow.ExecutionStatus.RUNNING) is False
    assert workflow.is_terminal(workflow.ExecutionStatus.RETRY_WAIT) is False
    assert workflow.is_terminal(workflow.ExecutionStatus.RECOVERING) is False


def test_fr_007_invalid_transition_is_rejected_with_audit_evidence() -> None:
    """A terminal execution cannot restart and the rejected attempt stays auditable."""

    workflow = _workflow_module()

    with pytest.raises(workflow.InvalidWorkflowTransition) as caught:
        workflow.transition_execution(
            workflow.ExecutionStatus.SUCCEEDED,
            workflow.ExecutionStatus.RUNNING,
        )

    assert caught.value.audit_event.event_type == "WORKFLOW_TRANSITION_REJECTED"
    assert caught.value.audit_event.payload == {
        "from_status": "SUCCEEDED",
        "to_status": "RUNNING",
        "reason_code": "INVALID_EXECUTION_TRANSITION",
    }


def test_fr_007_dependencies_define_conditions_and_stable_ordering() -> None:
    """Changing dependency outcomes or declaration order must change readiness."""

    workflow = _workflow_module()
    definition = workflow.WorkflowDefinition(
        steps=(
            workflow.WorkflowStep("extract"),
            workflow.WorkflowStep(
                "validate",
                dependencies=(
                    workflow.StepDependency(
                        "extract", frozenset({workflow.StepOutcome.SUCCEEDED})
                    ),
                ),
            ),
            workflow.WorkflowStep(
                "canonicalize",
                dependencies=(
                    workflow.StepDependency(
                        "validate",
                        frozenset(
                            {
                                workflow.StepOutcome.SUCCEEDED,
                                workflow.StepOutcome.SKIPPED,
                            }
                        ),
                    ),
                ),
            ),
        )
    )

    assert definition.ordered_step_names == ("extract", "validate", "canonicalize")
    assert definition.ready_step_names({}) == ("extract",)
    assert definition.ready_step_names(
        {"extract": workflow.StepOutcome.SUCCEEDED}
    ) == ("validate",)
    assert definition.ready_step_names(
        {
            "extract": workflow.StepOutcome.SUCCEEDED,
            "validate": workflow.StepOutcome.SKIPPED,
        }
    ) == ("canonicalize",)


@pytest.mark.parametrize(
    "steps",
    [
        (
            ("extract", ("missing",)),
            ("validate", ("extract",)),
        ),
        (
            ("extract", ("validate",)),
            ("validate", ("extract",)),
        ),
    ],
    ids=("unknown-dependency", "cycle"),
)
def test_fr_007_invalid_dependency_graph_is_rejected(
    steps: Iterable[tuple[str, tuple[str, ...]]],
) -> None:
    """Unknown or cyclic dependencies cannot create an executable workflow."""

    workflow = _workflow_module()
    definitions = tuple(
        workflow.WorkflowStep(
            name,
            dependencies=tuple(
                workflow.StepDependency(
                    dependency,
                    frozenset({workflow.StepOutcome.SUCCEEDED}),
                )
                for dependency in dependencies
            ),
        )
        for name, dependencies in steps
    )

    with pytest.raises(ValueError, match="dependency"):
        workflow.WorkflowDefinition(steps=definitions)


def test_fr_007_dependency_conditions_require_terminal_outcomes() -> None:
    """A running predecessor cannot be configured as a completed dependency condition."""

    workflow = _workflow_module()

    with pytest.raises(ValueError, match="terminal"):
        workflow.StepDependency(
            "extract",
            frozenset({workflow.StepOutcome.RUNNING}),
        )
