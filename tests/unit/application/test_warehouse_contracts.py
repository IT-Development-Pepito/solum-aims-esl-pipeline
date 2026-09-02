"""Typed warehouse-source boundary (#91, FR-001, FR-002, FR-026)."""

from datetime import UTC, datetime

import pytest

from esl_service.application.contracts import (
    SourceWindow,
    StoreDirectoryEntry,
    WarehouseProvenance,
    WarehouseReadRequest,
)
from esl_service.domain.serialization import canonical_payload

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)


def test_source_window_requires_ordered_timezone_aware_instants() -> None:
    assert SourceWindow(START, END).start == START

    with pytest.raises(ValueError, match="timezone-aware"):
        SourceWindow(START.replace(tzinfo=None), END)
    with pytest.raises(ValueError, match="must not follow"):
        SourceWindow(END, START)


def test_store_directory_entry_keeps_only_verified_routing_fields() -> None:
    store = StoreDirectoryEntry(
        store_code="084", org_ip="10.0.0.84", org_db="STORE_084"
    )

    assert (store.store_code, store.org_ip, store.org_db) == (
        "084",
        "10.0.0.84",
        "STORE_084",
    )


@pytest.mark.parametrize("field", ["store_code", "org_ip", "org_db"])
def test_store_directory_entry_rejects_blank_routing_fields(field: str) -> None:
    values = {"store_code": "084", "org_ip": "10.0.0.84", "org_db": "STORE_084"}
    values[field] = "  "

    with pytest.raises(ValueError, match=field):
        StoreDirectoryEntry(**values)


def test_read_request_and_provenance_retain_the_reproducible_window() -> None:
    window = SourceWindow(START, END)
    request = WarehouseReadRequest(store_code="084", source_window=window)
    provenance = WarehouseProvenance(
        instance="warehouse.internal",
        database="DBWH_8555",
        objects=("dbo.DimItemMapping", "dbo.FactCampaign"),
        query_version="warehouse-current-state-v1",
        source_window_start=START,
        source_window_end=END,
        source_watermark=END,
    )

    assert request.source_window == window
    assert provenance.source_window_start == START
    assert provenance.source_window_end == END
    assert provenance.source_watermark == END
    assert provenance.objects == ("dbo.DimItemMapping", "dbo.FactCampaign")
    assert canonical_payload(provenance) == {
        "instance": "warehouse.internal",
        "database": "DBWH_8555",
        "objects": ["dbo.DimItemMapping", "dbo.FactCampaign"],
        "query_version": "warehouse-current-state-v1",
        "source_window_start": "2026-09-02T01:00:00+00:00",
        "source_window_end": "2026-09-02T02:00:00+00:00",
        "source_watermark": "2026-09-02T02:00:00+00:00",
        "isolation_level": "READ COMMITTED",
    }
