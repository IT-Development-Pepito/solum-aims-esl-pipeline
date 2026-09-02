"""The PEPITO_HO UOM-mapping port (#93, FR-001, FR-002, FR-025).

`ITEM_UOM_MAPPING_MST` is a central table with no store column, so the only
honest bound a caller can give is the item set it needs mappings for. The
request therefore carries item codes and the reproducible source window;
the result carries raw rows and the same flat provenance the warehouse port
records (#91), so both tiers persist evidence identically.
"""

from datetime import UTC, datetime

import pytest

from esl_service.application.contracts import (
    SourceWindow,
    UomMappingReadRequest,
    UomMappingReadResult,
    UomMappingSourceReader,
    WarehouseProvenance,
)

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)


def window() -> SourceWindow:
    return SourceWindow(START, END)


def test_a_request_carries_the_item_set_and_window() -> None:
    request = UomMappingReadRequest(("SKU-1", "SKU-2"), window())

    assert request.item_codes == ("SKU-1", "SKU-2")
    assert request.source_window == window()


def test_item_codes_are_trimmed_and_deduplicated_preserving_order() -> None:
    request = UomMappingReadRequest((" SKU-2 ", "SKU-1", "SKU-2"), window())

    assert request.item_codes == ("SKU-2", "SKU-1")


def test_an_empty_item_set_is_refused() -> None:
    """An unbounded read of a central table is never what a caller means."""

    with pytest.raises(ValueError, match="item_codes"):
        UomMappingReadRequest((), window())


def test_a_blank_item_code_is_refused() -> None:
    with pytest.raises(ValueError, match="item_codes"):
        UomMappingReadRequest(("SKU-1", "  "), window())


def test_the_result_pairs_raw_rows_with_provenance() -> None:
    provenance = WarehouseProvenance(
        instance="192.168.85.18",
        database="PEPITO_HO",
        objects=("dbo.ITEM_UOM_MAPPING_MST",),
        query_version="pepito-ho-uom-current-state-v1",
        source_window_start=START,
        source_window_end=END,
        source_watermark=END,
    )

    result = UomMappingReadResult(mappings=({"IUM_ITM_CD": "SKU-1"},), provenance=provenance)

    assert result.mappings[0]["IUM_ITM_CD"] == "SKU-1"
    assert result.provenance.database == "PEPITO_HO"


def test_the_port_is_runtime_checkable_and_read_only() -> None:
    class Reader:
        def read_mappings(self, request: UomMappingReadRequest) -> UomMappingReadResult:
            raise NotImplementedError

    assert isinstance(Reader(), UomMappingSourceReader)
    assert [name for name in dir(UomMappingSourceReader) if not name.startswith("_")] == [
        "read_mappings"
    ]
