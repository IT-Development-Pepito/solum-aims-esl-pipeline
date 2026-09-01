"""Operator workflow controls and status query boundaries (FR-011, FR-012)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from esl_service.domain.operations import (
    ExecutionQuery,
    InvalidExecutionQuery,
    InvalidWorkflowControl,
    ReplayRequest,
    RetryRefusalReason,
    RetryRequest,
    decide_retry,
)
from esl_service.domain.workflow import ExecutionStatus

WINDOW_START = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("field", ["requested_by", "reason"])
def test_retry_requires_operator_identity_and_reason(field: str) -> None:
    """FR-011: an anonymous or unexplained retry cannot be requested."""

    values = {"requested_by": "ops.alice", "reason": "INC-1234"}
    values[field] = "  "

    with pytest.raises(InvalidWorkflowControl, match=field):
        RetryRequest(**values)


def test_only_a_failed_execution_is_retryable() -> None:
    """FR-011: retry creates work only from the explicit FAILED state."""

    accepted = decide_retry(
        ExecutionStatus.FAILED, has_unresolved_external_action=False
    )
    refused = decide_retry(
        ExecutionStatus.RUNNING, has_unresolved_external_action=False
    )

    assert accepted.allowed is True
    assert accepted.refusal is None
    assert refused.allowed is False
    assert refused.refusal is RetryRefusalReason.EXECUTION_NOT_FAILED


def test_an_unresolved_external_action_blocks_retry() -> None:
    """FR-011/FR-013: an ambiguous external outcome is never resent blindly."""

    decision = decide_retry(ExecutionStatus.FAILED, has_unresolved_external_action=True)

    assert decision.allowed is False
    assert decision.refusal is RetryRefusalReason.UNRESOLVED_EXTERNAL_ACTION


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, WINDOW_END),
        (WINDOW_START, None),
        (WINDOW_START.replace(tzinfo=None), WINDOW_END),
        (WINDOW_START, WINDOW_END.replace(tzinfo=None)),
        (WINDOW_END, WINDOW_START),
    ],
)
def test_replay_requires_an_explicit_bounded_aware_window(
    start: datetime | None, end: datetime | None
) -> None:
    """FR-011: missing, naive, or inverted historical replay is rejected."""

    with pytest.raises(InvalidWorkflowControl):
        ReplayRequest(
            requested_by="ops.alice",
            reason="INC-1234 corrected source",
            source_window_start=start,
            source_window_end=end,
        )


def test_execution_query_accepts_composable_status_selectors() -> None:
    """FR-012: execution, workflow, store, and time filters are explicit."""

    query = ExecutionQuery(
        execution_id=uuid4(),
        workflow_name="esl-refresh",
        store_code="084",
        started_from=WINDOW_START,
        started_to=WINDOW_END,
    )

    assert query.workflow_name == "esl-refresh"


@pytest.mark.parametrize(
    "query",
    [
        ExecutionQuery,
        lambda: ExecutionQuery(workflow_name="  "),
        lambda: ExecutionQuery(started_from=WINDOW_START),
        lambda: ExecutionQuery(started_to=WINDOW_END),
        lambda: ExecutionQuery(
            started_from=WINDOW_END,
            started_to=WINDOW_START,
        ),
    ],
)
def test_execution_query_rejects_unbounded_or_invalid_filters(query: object) -> None:
    """FR-012: a status query cannot silently become an unbounded table scan."""

    with pytest.raises(InvalidExecutionQuery):
        query()  # type: ignore[operator]
