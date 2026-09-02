"""The tb_ESL parity-baseline port (#94, FR-021, FR-022).

``tb_ESL`` is what the legacy procedure writes. It is not a source: the
replacement reads the same three tiers the procedure reads and computes in
the domain. Under ``ESL_SHADOW_MODE`` its rows are read as the baseline the
computed canonical records are compared against, and only for that.
"""

from datetime import UTC, datetime

import pytest

from esl_service.application.contracts import (
    BaselineReadRequest,
    BaselineReadResult,
    LegacyBaselineReader,
    WarehouseProvenance,
)

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)


def test_a_request_names_one_store_and_its_window() -> None:
    from esl_service.application.contracts import SourceWindow

    request = BaselineReadRequest("084", SourceWindow(START, END))

    assert request.store_code == "084"
    assert request.source_window.start == START


def test_a_blank_store_code_is_refused() -> None:
    from esl_service.application.contracts import SourceWindow

    with pytest.raises(ValueError, match="store_code"):
        BaselineReadRequest(" ", SourceWindow(START, END))


def test_the_result_pairs_raw_rows_with_provenance() -> None:
    provenance = WarehouseProvenance(
        instance="sql.internal",
        database="ESL",
        objects=("dbo.tb_ESL",),
        query_version="tb-esl-baseline-v1",
        source_window_start=START,
        source_window_end=END,
        source_watermark=END,
    )

    result = BaselineReadResult(rows=({"STORE_CODE": "084", "ITEM_CODE": "SKU-1"},), provenance=provenance)

    assert result.rows[0]["ITEM_CODE"] == "SKU-1"
    assert result.provenance.objects == ("dbo.tb_ESL",)


def test_the_port_is_runtime_checkable_and_read_only() -> None:
    class Reader:
        def read_baseline(self, request: BaselineReadRequest) -> BaselineReadResult:
            raise NotImplementedError

    assert isinstance(Reader(), LegacyBaselineReader)
    assert [n for n in dir(LegacyBaselineReader) if not n.startswith("_")] == ["read_baseline"]
