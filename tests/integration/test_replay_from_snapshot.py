"""Reproduce a run from its retained canonical capture without any source read (#114).

Raw source rows are not retained (AD-005), so a snapshot replay cannot
re-canonicalize; it re-persists the retained canonical records under the
original's configuration and rule versions, proves the aggregate hash
reproduces, records how the capture differs from the store's current
expected state, and never intends an action.
"""

# ruff: noqa: F811 - `repositories` is a fixture imported from the persist tests, then used by name

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.application.persist_run import (
    DIFFERENCE_CHANGED,
    REPRESENTATION_SOURCE_EXPECTED,
)
from esl_service.application.replay import (
    EVENT_SNAPSHOT_REPLAYED,
    ReplayContext,
    ReplaySourceMissing,
    replay_from_snapshot,
)
from esl_service.domain.operations import SnapshotReplayRequest
from esl_service.domain.outcomes import ExecutionMode
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import (
    ExecutionEvent,
    RecordAction,
    RecordDifference,
    SnapshotSet,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.integration.test_persist_run import (  # noqa: F401 - `repositories` is a fixture
    NOW,
    new_run,
    record,
    repositories,
    result_of,
    run,
)


def _replay_of(session: Session, original_id: UUID) -> UUID:
    launched = LaunchRepository(session).launch_snapshot_replay(
        original_id,
        SnapshotReplayRequest(requested_by="ops.alice", reason="INC-9 reproduce"),
        correlation_id=uuid4(),
    )
    assert launched.execution is not None
    session.flush()
    return launched.execution.id


def _replay(session: Session, replay_id: UUID, original_id: UUID, *, step_id: UUID | None = None):
    return replay_from_snapshot(
        ReplayContext(
            execution_id=replay_id,
            replay_of_execution_id=original_id,
            store_code="084",
            mode=ExecutionMode.SHADOW,
            rule_version="rules-v1",
        ),
        executions=ExecutionRepository(session),
        snapshots=SnapshotRepository(session),
        reconciliation=ReconciliationRepository(session),
        clock=lambda: NOW,
        step_id=step_id,
    )


def test_a_replay_reproduces_the_capture_hash_and_reads_no_source(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    original_id = new_run(session, repositories, configuration_version_id)
    original = run(repositories, original_id, result_of(record("A"), record("B")))
    replay_id = _replay_of(session, original_id)

    replayed = _replay(session, replay_id, original_id)

    source = session.get_one(SnapshotSet, original.snapshot_set_id)
    copy = session.get_one(SnapshotSet, replayed.snapshot_set_id)
    assert replayed.source_snapshot_set_id == source.id
    assert copy.execution_id == replay_id
    assert copy.representation_kind == REPRESENTATION_SOURCE_EXPECTED
    assert copy.record_count == 2
    assert copy.aggregate_hash == source.aggregate_hash
    assert replayed.hash_reproduced is True
    # the store's current expected state is the original itself: nothing changed
    assert replayed.differences.unchanged == 2 and replayed.differences.changed == 0
    # a replay is evidence, never a mutation path
    assert session.scalar(select(RecordAction.id).where(RecordAction.execution_id == replay_id)) is None
    event = session.scalars(
        select(ExecutionEvent).where(
            ExecutionEvent.execution_id == replay_id, ExecutionEvent.event_type == EVENT_SNAPSHOT_REPLAYED
        )
    ).one()
    assert event.payload["source_snapshot_set_id"] == str(source.id)
    assert event.payload["hash_reproduced"] is True
    assert event.payload["records"] == 2


def test_a_replay_report_is_balanced_and_counts_a_changed_record_as_ineligible(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    """After a later run changed a price, replaying the older capture shows the drift."""

    older_id = new_run(session, repositories, configuration_version_id)
    run(repositories, older_id, result_of(record("A", Decimal(50000)), record("B")))
    newer_id = new_run(session, repositories, configuration_version_id)
    newer = run(repositories, newer_id, result_of(record("A", Decimal(50001)), record("B")))
    # Inside one rolled-back transaction every capture shares now(); make the
    # newer capture demonstrably later, as separate committed runs would be.
    session.get_one(SnapshotSet, newer.snapshot_set_id).captured_at = datetime.now(UTC) + timedelta(minutes=5)
    session.flush()
    replay_id = _replay_of(session, older_id)

    replayed = _replay(session, replay_id, older_id)

    assert replayed.differences.changed == 1 and replayed.differences.unchanged == 1
    report = ReconciliationRepository(session).latest_report(replay_id)
    assert report is not None and report.finalized_at is not None
    assert (report.extracted, report.valid, report.ineligible, report.eligible, report.unchanged, report.intended) == (
        2, 2, 1, 1, 1, 0
    )
    changed = session.scalars(
        select(RecordDifference).where(
            RecordDifference.execution_id == replay_id, RecordDifference.difference_type == DIFFERENCE_CHANGED
        )
    ).one()
    assert any(path.endswith("source_regular_price") for path in changed.changed_paths)


def test_a_replay_that_restarts_returns_its_existing_capture(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    original_id = new_run(session, repositories, configuration_version_id)
    run(repositories, original_id, result_of(record("A")))
    replay_id = _replay_of(session, original_id)
    first = _replay(session, replay_id, original_id)

    second = _replay(session, replay_id, original_id)

    assert second.resumed is True and second.snapshot_set_id == first.snapshot_set_id
    assert session.scalar(
        select(SnapshotSet.id).where(SnapshotSet.execution_id == replay_id, SnapshotSet.id != first.snapshot_set_id)
    ) is None


def test_a_replay_whose_source_capture_vanished_is_named_not_invented(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    original_id = new_run(session, repositories, configuration_version_id)
    replay_id = new_run(session, repositories, configuration_version_id)  # linked in name only

    try:
        _replay(session, replay_id, original_id)
    except ReplaySourceMissing as missing:
        assert str(original_id) in str(missing)
    else:
        raise AssertionError("a replay without a finalized source capture must refuse")
