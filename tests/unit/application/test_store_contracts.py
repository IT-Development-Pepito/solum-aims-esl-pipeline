"""The per-store iRetail port and its fan-out report (#92, FR-001/002/025/026).

One store's server is addressed from a ``DimStore`` row (#91), read as a
whole under one transaction, and reported individually: a store that cannot
be addressed, reached, or read is an outcome in the report, never an
exception that stops the other stores.
"""

from datetime import UTC, datetime

import pytest

from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreFanOutReport,
    StoreReadOutcome,
    StoreReadRequest,
    StoreReadResult,
    StoreSourceReader,
    WarehouseProvenance,
)
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.health import HealthState

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
STORE = StoreDirectoryEntry("084", "10.0.0.84", "STORE_084")


def window() -> SourceWindow:
    return SourceWindow(START, END)


def provenance() -> WarehouseProvenance:
    return WarehouseProvenance(
        instance="10.0.0.84",
        database="STORE_084",
        objects=STORE_OBJECTS,
        query_version="store-current-state-v1",
        source_window_start=START,
        source_window_end=END,
        source_watermark=END,
    )


def empty_result() -> StoreReadResult:
    return StoreReadResult(
        items=(),
        item_descriptions=(),
        campaign_headers=(),
        campaign_org_details=(),
        campaign_item_group_headers=(),
        campaign_item_group_conditions=(),
        campaign_condition_masters=(),
        campaign_item_group_details=(),
        stock=(),
        offline_movements=(),
        pos_offline_movements=(),
        selling_prices=(),
        provenance=provenance(),
    )


def test_discovery_separates_routable_stores_from_unroutable_rows() -> None:
    """A DimStore row without ORG_IP/ORG_DB is reported, not raised (VERIFIED 2026-09-02)."""

    from esl_service.application.contracts import StoreDiscoveryResult, UnroutableStore

    result = StoreDiscoveryResult(
        stores=(STORE,),
        provenance=provenance(),
        unroutable=(UnroutableStore("001", "ORG_IP and ORG_DB are missing"),),
    )

    assert result.stores == (STORE,)
    assert result.unroutable[0].store_code == "001"
    assert StoreDiscoveryResult(stores=(), provenance=provenance()).unroutable == ()
    with pytest.raises(ValueError):
        UnroutableStore("001", " ")


def test_the_twelve_store_objects_are_named_once() -> None:
    assert len(STORE_OBJECTS) == 12
    assert STORE_OBJECTS[0] == "dbo.ITEM_MST"
    assert "dbo.BASIC_SP_MST" in STORE_OBJECTS


def test_a_request_addresses_one_store_with_its_window() -> None:
    request = StoreReadRequest(STORE, window())

    assert request.store.store_code == "084"
    assert request.source_window == window()


def test_the_result_carries_one_tuple_per_object() -> None:
    result = empty_result()

    assert len(result.as_mapping()) == 12
    assert set(result.as_mapping()) == set(STORE_OBJECTS)


def test_the_port_is_runtime_checkable_and_read_only() -> None:
    class Reader:
        def read_store(self, request: StoreReadRequest) -> StoreReadResult:
            raise NotImplementedError

    assert isinstance(Reader(), StoreSourceReader)
    assert [n for n in dir(StoreSourceReader) if not n.startswith("_")] == ["read_store"]


# --- outcomes and the fan-out report --------------------------------------------


def test_an_outcome_is_exactly_one_of_read_failed_or_skipped() -> None:
    read = StoreReadOutcome.read("084", empty_result())
    failed = StoreReadOutcome.failed(
        "085", FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)
    )
    skipped = StoreReadOutcome.skipped("086", "INVALID_STORE_ADDRESS")

    assert (read.succeeded, failed.succeeded, skipped.succeeded) == (True, False, False)
    assert failed.failure is not None and skipped.skipped_reason == "INVALID_STORE_ADDRESS"
    with pytest.raises(ValueError):
        StoreReadOutcome(
            store_code="087",
            result=empty_result(),
            failure=FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE),
            skipped_reason=None,
        )


def test_the_report_partitions_outcomes_and_keeps_store_order() -> None:
    report = StoreFanOutReport(
        (
            StoreReadOutcome.skipped("086", "INVALID_STORE_ADDRESS"),
            StoreReadOutcome.read("084", empty_result()),
            StoreReadOutcome.failed(
                "085", FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)
            ),
        )
    )

    assert [o.store_code for o in report.outcomes] == ["084", "085", "086"]
    assert [o.store_code for o in report.succeeded] == ["084"]
    assert [o.store_code for o in report.failed] == ["085"]
    assert [o.store_code for o in report.skipped] == ["086"]


def test_a_duplicate_store_in_one_report_is_refused() -> None:
    with pytest.raises(ValueError, match="084"):
        StoreFanOutReport(
            (StoreReadOutcome.read("084", empty_result()), StoreReadOutcome.read("084", empty_result()))
        )


def test_the_report_becomes_one_dependency_health_per_store() -> None:
    """Acceptance: each store is reported individually through the #27 report."""

    report = StoreFanOutReport(
        (
            StoreReadOutcome.read("084", empty_result()),
            StoreReadOutcome.failed(
                "085", FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED)
            ),
            StoreReadOutcome.skipped("086", "INVALID_STORE_ADDRESS"),
        )
    )

    health = report.dependency_health()

    assert [(h.name, h.state, h.required) for h in health] == [
        ("store-084", HealthState.HEALTHY, False),
        ("store-085", HealthState.UNAVAILABLE, False),
        ("store-086", HealthState.DEGRADED, False),
    ]
    assert health[1].detail == "credential expired"
    assert health[2].detail == "INVALID_STORE_ADDRESS"
