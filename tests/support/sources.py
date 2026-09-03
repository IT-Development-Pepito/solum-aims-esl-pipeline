"""A source port that fails on cue.

``ScriptedSources`` serves one small store, the same rows the runner's
end-to-end test uses, and raises whatever a scenario queued for a named read
the next time that read is called: a ``FailureSignal`` becomes a classified
``StepFailure``; any other exception is raised as it is, which is how a
scenario stands in for a power loss or an adapter error the matrix does not
know.
"""

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from esl_service.application.contracts import (
    STORE_OBJECTS,
    BaselineReadResult,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadResult,
    WarehouseProvenance,
    WarehouseReadResult,
)
from esl_service.application.runner import (
    STEP_DISCOVER,
    STEP_READ_PEPITO_HO,
    STEP_READ_STORE,
    STEP_READ_WAREHOUSE,
    StepFailure,
)
from esl_service.domain.failures import FailureSignal

STORE = StoreDirectoryEntry("084", "10.0.0.84", "PEPITO_084")
READ_BASELINE = "read-baseline"


def _provenance(instance: str, database: str, objects: tuple[str, ...], window: SourceWindow) -> WarehouseProvenance:
    return WarehouseProvenance(instance, database, objects, "test-v1", window.start, window.end, window.end)


@dataclass
class ScriptedSources:
    """The four tiers as fakes, each read able to fail once on cue."""

    faults: dict[str, deque[BaseException]] = field(default_factory=lambda: defaultdict(deque))
    calls: list[str] = field(default_factory=list)

    def fail_next(self, step: str, failure: FailureSignal | BaseException) -> None:
        """Queue one failure for the next call of ``step``."""

        error = StepFailure(step, failure) if isinstance(failure, FailureSignal) else failure
        self.faults[step].append(error)

    def _serve(self, step: str) -> None:
        self.calls.append(step)
        queued = self.faults.get(step)
        if queued:
            raise queued.popleft()

    def discover_store(self, store_code: str, window: SourceWindow) -> StoreDirectoryEntry | None:
        self._serve(STEP_DISCOVER)
        return STORE

    def read_warehouse(self, store_code: str, window: SourceWindow) -> WarehouseReadResult:
        self._serve(STEP_READ_WAREHOUSE)
        return WarehouseReadResult(
            (), (), _provenance("sql.internal", "DBWH_8555", ("dbo.DimItemMapping", "dbo.FactCampaign"), window)
        )

    def read_uom_mappings(self, item_codes: Sequence[str], window: SourceWindow) -> UomMappingReadResult:
        self._serve(STEP_READ_PEPITO_HO)
        return UomMappingReadResult((), _provenance("192.168.85.18", "PEPITO_HO", ("dbo.ITEM_UOM_MAPPING_MST",), window))

    def read_store(self, entry: StoreDirectoryEntry, window: SourceWindow) -> StoreReadResult:
        self._serve(STEP_READ_STORE)
        rows: dict[str, tuple[dict[str, object], ...]] = {name: () for name in STORE_OBJECTS}
        rows["dbo.ITEM_MST"] = (
            {"ITM_CD": "SKU-1", "ITM_STATUS": "O", "ITM_SALES_UOM": "PCS", "ITM_LONG_NAME": "Item one"},
            {"ITM_CD": "SKU-2", "ITM_STATUS": "C", "ITM_SALES_UOM": "PCS"},
        )
        rows["dbo.BASIC_SP_MST"] = (
            {"BSP_ITEM_CD": "SKU-1", "BSP_UOM": "PCS", "BSP_SELL_PRICE": Decimal(12500), "BSP_PRICE_CATG": "001", "BSP_STATUS": "A"},
        )
        return StoreReadResult.from_mapping(rows, _provenance(entry.org_ip, entry.org_db, STORE_OBJECTS, window))

    def read_baseline(self, store_code: str, window: SourceWindow) -> BaselineReadResult | None:
        self._serve(READ_BASELINE)
        return None
