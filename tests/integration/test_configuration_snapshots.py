"""Integration coverage for configuration, canonical snapshots, and differences.

Requirements: FR-026 configured multi-store processing, FR-027 deterministic
comparison from durable state rather than CSV files, and BR-018 the
``store_code + item_code + selling_uom`` canonical boundary.
"""

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from esl_service.domain import canonical_hash, canonical_payload, diff_records
from esl_service.domain.diff import diff_payloads
from esl_service.persistence.models import (
    CanonicalRecordSnapshot,
    ConfigurationVersion,
    StoreConfiguration,
    WorkflowExecution,
)
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

WATERMARK = "2026-08-28T07:00:00+00:00"


def _create_execution(
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
    store_code: str = "084",
) -> WorkflowExecution:
    """Create one execution that owns the snapshot evidence under test."""

    return execution_repository.create_execution(
        new_execution(configuration_version_id, store_code=store_code)
    )


def _create_snapshot_set(
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
    *,
    store_code: str = "084",
    representation_kind: str = "SOURCE_EXPECTED",
    source_watermark: str = WATERMARK,
):
    """Create one snapshot set attached to a fresh execution."""

    execution = _create_execution(
        execution_repository, configuration_version_id, store_code
    )
    return snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind=representation_kind,
        adapter_name="sqlserver",
        source_watermark=source_watermark,
        canonical_schema_version="canonical-v1",
    )


def test_snapshot_round_trip_preserves_complete_record(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """A persisted snapshot reloads with an identical payload, hash, and key."""

    snapshot_set = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id
    )
    source = canonical_record()

    snapshot_repository.append_record(snapshot_set.id, source)
    session.flush()
    session.expire_all()

    loaded = snapshot_repository.list_records(snapshot_set.id)[0]
    assert loaded.canonical_hash == canonical_hash(source)
    assert loaded.payload == canonical_payload(source)
    assert (loaded.store_code, loaded.item_code, loaded.selling_uom) == (
        "084",
        "101024011793",
        "KGS",
    )


def test_snapshot_key_is_unique_per_set(
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """One snapshot set cannot hold the same canonical key twice (BR-018)."""

    snapshot_set = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id
    )
    snapshot_repository.append_record(snapshot_set.id, canonical_record())

    with pytest.raises(IntegrityError):
        snapshot_repository.append_record(snapshot_set.id, canonical_record())


def test_same_item_and_uom_are_isolated_per_store(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """The same item and UOM in two stores are separate evidence (FR-026, BR-018)."""

    store_075 = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id, store_code="075"
    )
    store_084 = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id, store_code="084"
    )

    snapshot_repository.append_record(
        store_075.id, canonical_record(store_code="075", source_regular_price=Decimal(51000))
    )
    snapshot_repository.append_record(store_084.id, canonical_record(store_code="084"))
    session.flush()
    session.expire_all()

    record_075 = snapshot_repository.list_records(store_075.id)[0]
    record_084 = snapshot_repository.list_records(store_084.id)[0]
    assert record_075.store_code == "075"
    assert record_084.store_code == "084"
    assert record_075.canonical_hash != record_084.canonical_hash


def test_same_item_is_isolated_per_selling_uom(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """One item sold in two UOMs keeps separate canonical rows (BR-018)."""

    snapshot_set = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id
    )

    snapshot_repository.append_record(snapshot_set.id, canonical_record(selling_uom="KGS"))
    snapshot_repository.append_record(snapshot_set.id, canonical_record(selling_uom="PCS"))
    session.flush()
    session.expire_all()

    stored = snapshot_repository.list_records(snapshot_set.id)
    assert sorted(record.selling_uom for record in stored) == ["KGS", "PCS"]


def test_configured_stores_extend_without_a_code_change(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """A third configured store processes through the same code path (FR-026)."""

    for store_code in ("075", "084", "091"):
        session.add(
            StoreConfiguration(
                store_code=store_code,
                display_name=f"Store {store_code}",
                timezone="Asia/Jakarta",
                enabled=True,
                options_schema_version="store-options-v1",
                options={"page_profile": "default"},
            )
        )
    session.flush()

    configured = _enabled_store_codes(session)
    assert configured == ["075", "084", "091"]

    for store_code in configured:
        snapshot_set = _create_snapshot_set(
            snapshot_repository,
            execution_repository,
            configuration_version_id,
            store_code=store_code,
        )
        snapshot_repository.append_record(
            snapshot_set.id, canonical_record(store_code=store_code)
        )
        session.flush()
        assert snapshot_repository.list_records(snapshot_set.id)[0].store_code == store_code


def test_durable_state_reproduces_the_same_comparison(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """Reloaded snapshots reproduce the in-memory diff without a CSV file (FR-027)."""

    execution = _create_execution(execution_repository, configuration_version_id)
    expected_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark=WATERMARK,
        canonical_schema_version="canonical-v1",
    )
    observed_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="AIMS_OBSERVED",
        adapter_name="aims-read",
        source_watermark=WATERMARK,
        canonical_schema_version="canonical-v1",
    )

    expected = canonical_record()
    observed = canonical_record(source_regular_price=Decimal(52000))
    expected_row = snapshot_repository.append_record(expected_set.id, expected)
    observed_row = snapshot_repository.append_record(observed_set.id, observed)

    in_memory = diff_records(expected, observed)
    assert [difference.path for difference in in_memory] == [
        "pricing.source_regular_price"
    ]

    snapshot_repository.append_difference(
        execution_id=execution.id,
        left_snapshot_id=expected_row.id,
        right_snapshot_id=observed_row.id,
        difference_type="CHANGED",
        differences=in_memory,
        diff_schema_version="diff-v1",
        rule_version="rules-v1",
    )
    session.flush()
    session.expire_all()

    reloaded_expected = snapshot_repository.list_records(expected_set.id)[0]
    reloaded_observed = snapshot_repository.list_records(observed_set.id)[0]
    reproduced = diff_payloads(reloaded_expected.payload, reloaded_observed.payload)
    assert reproduced == in_memory

    stored_difference = snapshot_repository.list_differences(execution.id)[0]
    assert stored_difference.changed_paths == ["pricing.source_regular_price"]
    assert stored_difference.left_hash == reloaded_expected.canonical_hash
    assert stored_difference.right_hash == reloaded_observed.canonical_hash
    assert stored_difference.values_payload == {
        "pricing.source_regular_price": {"old": "50000", "new": "52000"}
    }


def test_snapshot_set_is_unique_per_representation_and_watermark(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """One execution cannot capture the same representation window twice."""

    execution = _create_execution(execution_repository, configuration_version_id)
    snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark=WATERMARK,
        canonical_schema_version="canonical-v1",
    )
    with pytest.raises(IntegrityError):
        snapshot_repository.create_snapshot_set(
            execution_id=execution.id,
            representation_kind="SOURCE_EXPECTED",
            adapter_name="sqlserver",
            source_watermark=WATERMARK,
            canonical_schema_version="canonical-v1",
        )


def test_finalized_snapshot_set_records_count_and_aggregate_hash(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """Sealing a capture records its size and a deterministic aggregate hash."""

    snapshot_set = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id
    )
    snapshot_repository.append_record(snapshot_set.id, canonical_record(selling_uom="KGS"))
    snapshot_repository.append_record(snapshot_set.id, canonical_record(selling_uom="PCS"))

    finalized = snapshot_repository.finalize_snapshot_set(snapshot_set.id)
    session.flush()

    assert finalized.record_count == 2
    assert finalized.aggregate_hash is not None
    assert len(finalized.aggregate_hash) == 64
    assert (
        snapshot_repository.finalize_snapshot_set(snapshot_set.id).aggregate_hash
        == finalized.aggregate_hash
    )


def test_configuration_version_is_unique_per_environment_and_hash(
    session: Session,
) -> None:
    """An immutable configuration version is recorded once per content hash."""

    for _ in range(2):
        session.add(
            ConfigurationVersion(
                environment="development",
                schema_version="config-v1",
                content_hash="a" * 64,
                sanitized_snapshot={"stores": ["075", "084"]},
                activated_by="operator",
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_execution_with_snapshot_evidence_cannot_be_deleted(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """Durable evidence uses RESTRICT so audit history survives deletion attempts."""

    execution = _create_execution(execution_repository, configuration_version_id)
    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution.id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark=WATERMARK,
        canonical_schema_version="canonical-v1",
    )
    snapshot_repository.append_record(snapshot_set.id, canonical_record())
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            delete(WorkflowExecution).where(WorkflowExecution.id == execution.id)
        )
        session.flush()


def test_snapshot_payload_must_be_a_json_object(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> None:
    """The database refuses a canonical payload that is not a JSON object."""

    snapshot_set = _create_snapshot_set(
        snapshot_repository, execution_repository, configuration_version_id
    )
    session.add(
        CanonicalRecordSnapshot(
            snapshot_set_id=snapshot_set.id,
            store_code="084",
            item_code="101024011793",
            selling_uom="KGS",
            canonical_schema_version="canonical-v1",
            canonical_hash="b" * 64,
            payload=["not", "an", "object"],
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def _enabled_store_codes(session: Session) -> Sequence[str]:
    """Return configured enabled store codes in deterministic order (FR-026)."""

    statement = (
        select(StoreConfiguration.store_code)
        .where(StoreConfiguration.enabled.is_(True))
        .order_by(StoreConfiguration.store_code)
    )
    return list(session.scalars(statement))
