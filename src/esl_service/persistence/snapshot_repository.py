"""Repository for canonical snapshot and comparison evidence.

Every method flushes so callers receive a durable identifier, and none commits
the caller's transaction. Persisted snapshots are the reproducible comparison
input required by FR-027; no physical file participates.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.domain.canonical import CanonicalEslRecord
from esl_service.domain.diff import FieldDifference
from esl_service.domain.serialization import canonical_hash, canonical_payload
from esl_service.persistence.models import (
    CanonicalRecordSnapshot,
    RecordDifference,
    SnapshotSet,
)


class SnapshotRepository:
    """Persists immutable canonical snapshots and their deterministic differences."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_snapshot_set(
        self,
        *,
        execution_id: UUID,
        representation_kind: str,
        adapter_name: str,
        source_watermark: str,
        canonical_schema_version: str,
    ) -> SnapshotSet:
        """Open one immutable capture of a single representation for an execution."""

        snapshot_set = SnapshotSet(
            execution_id=execution_id,
            representation_kind=representation_kind,
            adapter_name=adapter_name,
            source_watermark=source_watermark,
            canonical_schema_version=canonical_schema_version,
            record_count=0,
        )
        self._session.add(snapshot_set)
        self._session.flush()
        return snapshot_set

    def append_record(
        self, snapshot_set_id: UUID, record: CanonicalEslRecord
    ) -> CanonicalRecordSnapshot:
        """Persist one complete canonical record keyed by store, item, and UOM."""

        snapshot_set = self._session.get_one(SnapshotSet, snapshot_set_id)
        stored = CanonicalRecordSnapshot(
            snapshot_set_id=snapshot_set_id,
            store_code=record.key.store_code,
            item_code=record.key.item_code,
            selling_uom=record.key.selling_uom,
            canonical_schema_version=record.schema_version,
            canonical_hash=canonical_hash(record),
            payload=canonical_payload(record),
        )
        self._session.add(stored)
        snapshot_set.record_count += 1
        self._session.flush()
        return stored

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
    ) -> RecordDifference:
        """Persist path-level comparison evidence between two canonical snapshots."""

        difference = RecordDifference(
            execution_id=execution_id,
            left_snapshot_id=left_snapshot_id,
            right_snapshot_id=right_snapshot_id,
            left_hash=self._hash_of(left_snapshot_id),
            right_hash=self._hash_of(right_snapshot_id),
            difference_type=difference_type,
            changed_paths=[entry.path for entry in differences],
            values_payload={
                entry.path: {"old": entry.old_value, "new": entry.new_value}
                for entry in differences
            },
            diff_schema_version=diff_schema_version,
            rule_version=rule_version,
        )
        self._session.add(difference)
        self._session.flush()
        return difference

    def finalize_snapshot_set(self, snapshot_set_id: UUID) -> SnapshotSet:
        """Seal a capture with its record count and deterministic aggregate hash."""

        records = self.list_records(snapshot_set_id)
        snapshot_set = self._session.get_one(SnapshotSet, snapshot_set_id)
        snapshot_set.record_count = len(records)
        snapshot_set.aggregate_hash = canonical_hash(
            tuple(record.canonical_hash for record in records)
        )
        self._session.flush()
        return snapshot_set

    def list_records(self, snapshot_set_id: UUID) -> list[CanonicalRecordSnapshot]:
        """Return one capture's records ordered by their canonical business key."""

        statement = (
            select(CanonicalRecordSnapshot)
            .where(CanonicalRecordSnapshot.snapshot_set_id == snapshot_set_id)
            .order_by(
                CanonicalRecordSnapshot.store_code,
                CanonicalRecordSnapshot.item_code,
                CanonicalRecordSnapshot.selling_uom,
            )
        )
        return list(self._session.scalars(statement))

    def list_differences(self, execution_id: UUID) -> list[RecordDifference]:
        """Return an execution's comparison evidence in stable creation order."""

        statement = (
            select(RecordDifference)
            .where(RecordDifference.execution_id == execution_id)
            .order_by(RecordDifference.created_at, RecordDifference.id)
        )
        return list(self._session.scalars(statement))

    def _hash_of(self, snapshot_id: UUID | None) -> str | None:
        """Return a persisted snapshot's canonical hash, or None when absent."""

        if snapshot_id is None:
            return None
        return self._session.get_one(CanonicalRecordSnapshot, snapshot_id).canonical_hash
