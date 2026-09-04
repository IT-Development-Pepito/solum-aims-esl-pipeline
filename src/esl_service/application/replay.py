"""Reproduce a run from its retained canonical capture, reading no source (#114).

Raw source rows are not retained (AD-005), so a snapshot replay cannot
re-canonicalize or re-evaluate promotions. What it can do, and all it does,
is re-persist the original's finalized ``SOURCE_EXPECTED`` records verbatim
under the original's configuration and rule versions and then prove two
things from durable state alone: that the capture's aggregate hash
reproduces (retention is complete and the canonical serializer is stable,
NFR-002, NFR-012), and how that capture differs from the store's current
expected state. It never intends an action: a replay is evidence, not a
mutation path, so a record that differs from the current state is counted
*ineligible* and its difference row carries the detail.

The step is idempotent the way ``persist_run`` is: the replay execution's
own finalized capture is the proof it already ran.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from esl_service.application.persist_run import (
    DIFF_SCHEMA_VERSION,
    DIFFERENCE_ADDED,
    DIFFERENCE_CHANGED,
    DIFFERENCE_REMOVED,
    DIFFERENCE_UNCHANGED,
    REPRESENTATION_SOURCE_EXPECTED,
    ActiveModeUnsupported,
    DiffCounts,
    ExecutionPort,
    PersistInterrupted,
    ReconciliationPort,
    SnapshotRow,
    SnapshotSetRow,
)
from esl_service.domain.diff import FieldDifference, diff_payloads
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.serialization import JSONValue

STEP_REPLAY_SNAPSHOT = "replay-snapshot"
EVENT_SNAPSHOT_REPLAYED = "SNAPSHOT_REPLAYED"
CHECKPOINT_REPLAY_SNAPSHOT = "replay-snapshot:capture"
CHECKPOINT_REPLAY_REPORT = "replay-snapshot:report"


class ReplaySourceMissing(LookupError):
    """The original's finalized SOURCE_EXPECTED capture does not exist (purged, or never finalized)."""


@dataclass(frozen=True)
class ReplayContext:
    execution_id: UUID
    replay_of_execution_id: UUID
    store_code: str
    mode: ExecutionMode
    rule_version: str


@dataclass(frozen=True)
class ReplayedRun:
    source_snapshot_set_id: UUID
    snapshot_set_id: UUID
    report_id: UUID
    records: int
    hash_reproduced: bool
    differences: DiffCounts
    resumed: bool


# --- ports -------------------------------------------------------------------------


class StoredRecordRow(SnapshotRow, Protocol):
    @property
    def store_code(self) -> str: ...

    @property
    def canonical_schema_version(self) -> str: ...


class ReplaySnapshotSetRow(SnapshotSetRow, Protocol):
    @property
    def adapter_name(self) -> str: ...

    @property
    def source_watermark(self) -> str: ...

    @property
    def canonical_schema_version(self) -> str: ...


class ReplaySnapshotPort(Protocol):
    def find_snapshot_set(
        self, execution_id: UUID, representation_kind: str
    ) -> ReplaySnapshotSetRow | None: ...

    def create_snapshot_set(
        self,
        *,
        execution_id: UUID,
        representation_kind: str,
        adapter_name: str,
        source_watermark: str,
        canonical_schema_version: str,
    ) -> SnapshotSetRow: ...

    def list_records(self, snapshot_set_id: UUID) -> Sequence[StoredRecordRow]: ...

    def copy_record(self, snapshot_set_id: UUID, stored: StoredRecordRow) -> SnapshotRow: ...

    def finalize_snapshot_set(self, snapshot_set_id: UUID) -> SnapshotSetRow: ...

    def previous_finalized_records(
        self, store_code: str, representation_kind: str, *, exclude_execution_id: UUID
    ) -> Sequence[SnapshotRow]: ...

    def append_difference(
        self,
        *,
        execution_id: UUID,
        difference_type: str,
        differences: Sequence[FieldDifference],
        diff_schema_version: str,
        rule_version: str,
        left_snapshot_id: UUID | None = None,
        right_snapshot_id: UUID | None = None,
    ) -> object: ...


# --- the step ----------------------------------------------------------------------


def replay_from_snapshot(
    context: ReplayContext,
    *,
    executions: ExecutionPort,
    snapshots: ReplaySnapshotPort,
    reconciliation: ReconciliationPort,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    step_id: UUID | None = None,
) -> ReplayedRun:
    """Re-persist the original's retained capture and report what it proves."""

    if context.mode is not ExecutionMode.SHADOW:
        raise ActiveModeUnsupported(
            "only SHADOW runs can be replayed until the AIMS mutation adapter (#23) exists"
        )
    source = snapshots.find_snapshot_set(context.replay_of_execution_id, REPRESENTATION_SOURCE_EXPECTED)
    if source is None or source.aggregate_hash is None:
        raise ReplaySourceMissing(
            f"execution {context.replay_of_execution_id} has no finalized "
            f"{REPRESENTATION_SOURCE_EXPECTED} capture to replay"
        )

    existing = snapshots.find_snapshot_set(context.execution_id, REPRESENTATION_SOURCE_EXPECTED)
    if existing is not None:
        if existing.aggregate_hash is None:
            raise PersistInterrupted(
                f"execution {context.execution_id} has an unfinalized snapshot set; "
                "it must be reconciled before the step runs again"
            )
        report = reconciliation.latest_report(context.execution_id)
        if report is None:
            raise PersistInterrupted(
                f"execution {context.execution_id} has a finalized capture but no report"
            )
        return ReplayedRun(
            source_snapshot_set_id=source.id,
            snapshot_set_id=existing.id,
            report_id=report.id,
            records=0,
            hash_reproduced=existing.aggregate_hash == source.aggregate_hash,
            differences=DiffCounts(0, 0, 0, 0),
            resumed=True,
        )

    # 1. the capture, copied verbatim from what was retained
    watermark = f"{source.source_watermark}|replay:{clock().isoformat()}"
    capture = snapshots.create_snapshot_set(
        execution_id=context.execution_id,
        representation_kind=REPRESENTATION_SOURCE_EXPECTED,
        adapter_name=f"snapshot-replay:{source.adapter_name}",
        source_watermark=watermark,
        canonical_schema_version=source.canonical_schema_version,
    )
    copies: list[SnapshotRow] = [
        snapshots.copy_record(capture.id, stored) for stored in snapshots.list_records(source.id)
    ]
    finalized = snapshots.finalize_snapshot_set(capture.id)
    reproduced = finalized.aggregate_hash == source.aggregate_hash
    executions.append_event(
        context.execution_id,
        EVENT_SNAPSHOT_REPLAYED,
        {
            "source_execution_id": str(context.replay_of_execution_id),
            "source_snapshot_set_id": str(source.id),
            "source_aggregate_hash": source.aggregate_hash,
            "replay_aggregate_hash": finalized.aggregate_hash,
            "hash_reproduced": reproduced,
            "records": len(copies),
        },
    )
    _checkpoint(
        executions, step_id, CHECKPOINT_REPLAY_SNAPSHOT, watermark,
        {"snapshot_set_id": str(capture.id), "records": len(copies), "hash_reproduced": reproduced},
    )

    # 2. differences against the store's current expected state, by hash
    previous = {
        (row.item_code, row.selling_uom): row
        for row in snapshots.previous_finalized_records(
            context.store_code, REPRESENTATION_SOURCE_EXPECTED, exclude_execution_id=context.execution_id
        )
    }
    counts = {DIFFERENCE_ADDED: 0, DIFFERENCE_CHANGED: 0, DIFFERENCE_REMOVED: 0, DIFFERENCE_UNCHANGED: 0}
    for copy in copies:
        prior = previous.pop((copy.item_code, copy.selling_uom), None)
        if prior is None:
            kind = DIFFERENCE_ADDED
            snapshots.append_difference(
                execution_id=context.execution_id, difference_type=kind, differences=(),
                diff_schema_version=DIFF_SCHEMA_VERSION, rule_version=context.rule_version,
                right_snapshot_id=copy.id,
            )
        elif prior.canonical_hash == copy.canonical_hash:
            kind = DIFFERENCE_UNCHANGED
        else:
            kind = DIFFERENCE_CHANGED
            snapshots.append_difference(
                execution_id=context.execution_id, difference_type=kind,
                differences=diff_payloads(_stored(prior.payload), _stored(copy.payload)),
                diff_schema_version=DIFF_SCHEMA_VERSION, rule_version=context.rule_version,
                left_snapshot_id=prior.id, right_snapshot_id=copy.id,
            )
        counts[kind] += 1
    for removed in previous.values():
        snapshots.append_difference(
            execution_id=context.execution_id, difference_type=DIFFERENCE_REMOVED, differences=(),
            diff_schema_version=DIFF_SCHEMA_VERSION, rule_version=context.rule_version,
            left_snapshot_id=removed.id,
        )
        counts[DIFFERENCE_REMOVED] += 1

    # 3. a balanced report in which nothing is intended: a replay never acts
    unchanged = counts[DIFFERENCE_UNCHANGED]
    drifted = counts[DIFFERENCE_ADDED] + counts[DIFFERENCE_CHANGED]
    report_counts = ReconciliationCounts(
        extracted=len(copies), rejected=0, valid=len(copies),
        ineligible=drifted, eligible=unchanged, unchanged=unchanged,
        skipped_idempotent=0, intended=0, acknowledged=0, rejected_by_aims=0,
        failed=0, unresolved=0, submitted=0, ambiguous=0,
    )
    report = reconciliation.finalize_report(context.execution_id, ReconciliationMode.SHADOW, report_counts)
    _checkpoint(executions, step_id, CHECKPOINT_REPLAY_REPORT, watermark, {"report_id": str(report.id)})

    return ReplayedRun(
        source_snapshot_set_id=source.id,
        snapshot_set_id=capture.id,
        report_id=report.id,
        records=len(copies),
        hash_reproduced=reproduced,
        differences=DiffCounts(
            counts[DIFFERENCE_ADDED], counts[DIFFERENCE_CHANGED], counts[DIFFERENCE_REMOVED], unchanged
        ),
        resumed=False,
    )


def _checkpoint(
    executions: ExecutionPort, step_id: UUID | None, key: str, watermark: str, payload: Mapping[str, object]
) -> None:
    if step_id is None:
        return
    executions.append_checkpoint(
        step_id, checkpoint_key=key, checkpoint_version=1, watermark=watermark, payload=payload
    )


def _stored(payload: Mapping[str, object]) -> JSONValue:
    from typing import cast

    return cast("JSONValue", dict(payload))


__all__ = [
    "CHECKPOINT_REPLAY_REPORT",
    "CHECKPOINT_REPLAY_SNAPSHOT",
    "EVENT_SNAPSHOT_REPLAYED",
    "STEP_REPLAY_SNAPSHOT",
    "ReplayContext",
    "ReplaySnapshotPort",
    "ReplaySourceMissing",
    "ReplayedRun",
    "replay_from_snapshot",
]
