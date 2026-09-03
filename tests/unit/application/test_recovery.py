"""The recovery report read model (#21, FR-016, NFR-002).

Four fields, derived from durable state and never stored separately: the
scope, the checkpoint a restart resumes from, the external uncertainty that
blocks completion, and the next operator action. Presentation is #109's.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from esl_service.application.recovery import (
    NO_ACTION,
    RecoveryReport,
    UncertainAction,
    recovery_report,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
WINDOW = (datetime(2026, 9, 3, 11, 0, tzinfo=UTC), datetime(2026, 9, 3, 11, 30, tzinfo=UTC))


@dataclass
class FakeCheckpoint:
    checkpoint_key: str
    watermark: str


@dataclass
class FakeStep:
    step_name: str
    attempt: int
    outcome: str
    checkpoints: list[FakeCheckpoint] = field(default_factory=list)


@dataclass
class FakeExecution:
    status: str
    terminal_reason: str | None = None
    retry_not_before: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    workflow_name: str = "esl-refresh"
    store_code: str = "084"
    source_window_start: datetime = WINDOW[0]
    source_window_end: datetime = WINDOW[1]


def done(step: str, watermark: str = "w") -> FakeStep:
    return FakeStep(step, 1, "SUCCEEDED", [FakeCheckpoint(f"{step}:done", watermark)])


def report(execution: FakeExecution, steps: list[FakeStep] | None = None, actions: list[UncertainAction] | None = None) -> RecoveryReport:
    return recovery_report(execution, steps or [], actions or [], now=NOW)


def test_scope_names_the_workflow_store_and_source_window() -> None:
    execution = FakeExecution("QUEUED")

    result = report(execution)

    assert result.execution_id == execution.id
    assert result.scope == "esl-refresh:084"
    assert (result.source_window_start, result.source_window_end) == WINDOW


def test_checkpoint_is_the_last_succeeded_step_and_resume_from_is_the_next() -> None:
    steps = [done("discover", "084"), done("read-warehouse", "2026-09-03T11:30:00+00:00"), FakeStep("read-store", 2, "FAILED")]

    result = report(FakeExecution("RETRY_WAIT", retry_not_before=NOW + timedelta(seconds=8)), steps)

    assert result.checkpoint == "read-warehouse:done @ 2026-09-03T11:30:00+00:00"
    assert result.resume_from == "read-store"


def test_a_run_with_no_step_yet_has_no_checkpoint_and_resumes_from_the_first_step() -> None:
    result = report(FakeExecution("QUEUED"))

    assert result.checkpoint is None
    assert result.resume_from == "discover"


def test_a_retry_that_is_due_later_needs_no_operator_and_warns_against_a_duplicate_run() -> None:
    result = report(FakeExecution("RETRY_WAIT", retry_not_before=NOW + timedelta(seconds=8)), [done("discover")])

    assert result.next_operator_action == (
        "None: the retry is due at 2026-09-03T12:00:08+00:00 and resumes from read-warehouse; "
        "do not launch a duplicate manual run."
    )


def test_a_recovering_run_is_resumed_by_startup_recovery() -> None:
    result = report(FakeExecution("RECOVERING"), [done("discover"), done("read-warehouse")])

    assert result.next_operator_action == (
        "None: startup recovery resumes from read-store after checkpoint read-warehouse:done @ w; "
        "do not launch a duplicate manual run."
    )


def test_exhausted_retries_ask_for_the_dependency_to_be_restored_then_a_retry() -> None:
    execution = FakeExecution("FAILED", terminal_reason="read-store:sql_server:unavailable:RETRYABLE:attempts_exhausted")

    result = report(execution, [done("discover"), done("read-warehouse"), FakeStep("read-store", 2, "FAILED")])

    assert result.next_operator_action == (
        "Restore sql_server (unavailable), then retry the run; it resumes from read-store."
    )


def test_a_non_retryable_failure_asks_for_correction_then_a_bounded_replay() -> None:
    execution = FakeExecution("FAILED", terminal_reason="canonicalize:source_data:malformed:NON_RETRYABLE")

    result = report(execution)

    assert result.next_operator_action == (
        "Correct source_data (malformed) through the approved process, then replay the bounded window; "
        "nothing is retried automatically."
    )


def test_an_unclassified_failure_asks_for_review() -> None:
    execution = FakeExecution("FAILED", terminal_reason="canonicalize:unexpected:ValueError:OPERATOR_ACTION_REQUIRED")

    result = report(execution)

    assert result.next_operator_action == (
        "Review canonicalize: canonicalize:unexpected:ValueError:OPERATOR_ACTION_REQUIRED; "
        "nothing is retried automatically."
    )


def test_external_uncertainty_takes_precedence_over_every_other_action() -> None:
    actions = [
        UncertainAction(uuid4(), "084:101024011793:KGS:1:page", "OUTCOME_UNKNOWN"),
        UncertainAction(uuid4(), "084:101024011794:PCS:1:page", "OUTCOME_UNKNOWN"),
    ]

    result = report(FakeExecution("SUCCEEDED_WITH_EXCEPTIONS"), actions=actions)

    assert result.external_uncertainty == tuple(actions)
    assert result.next_operator_action == (
        "Reconcile 2 action(s) whose external outcome is unknown before any resend; "
        "they are never resubmitted automatically."
    )


def test_a_clean_success_needs_nothing_and_exceptions_ask_for_a_review() -> None:
    assert report(FakeExecution("SUCCEEDED")).next_operator_action == NO_ACTION
    assert report(FakeExecution("SUCCEEDED_WITH_EXCEPTIONS")).next_operator_action == (
        "Review the reconciliation report's exceptions; the run itself is complete."
    )
