"""Application-layer ports for external source and action adapters (AD-002).

Everything the service needs from an external system is expressed here as a
typed request, typed result, and Protocol so domain and orchestration code
never depends on transport details (NFR-010). The read-only `DBWH_8555` port
retains raw warehouse rows and safe provenance (FR-001, FR-002, FR-025,
FR-026). AIMS remains a separate vendor-owned boundary (FR-018, FR-020,
AD-003).

Outcomes are stated in the domain's own vocabulary: ``DeliveryCertainty`` for
reconciliation (FR-021) and ``FailureSignal`` for classification and retry
(FR-015, architecture section 8). An adapter therefore reports what happened;
it never decides whether to retry, and it never asserts delivery it cannot
evidence.

Direct writes to an AIMS database are forbidden (AD-002). Mutation is
expressed only as a page change submitted through ``AimsPageClient``, and the
AIMS-side read model is read-only through ``AimsReadModelReader``.

Evidence:
- VERIFIED: the page-change request and receipt fields, from the approved
  foundation plan's documented vendor payload.
- UNKNOWN / NEEDS-DISCOVERY: the vendor's valid page range, and the remaining
  columns of the AIMS label read model. Neither is documented, so neither is
  constrained or modelled here. Selecting the mutation protocol and the
  compatibility query is explicitly out of scope for this boundary.

These ports are synchronous, matching every other module in the service. The
foundation plan's Task 5 sketch shows an ``async def`` adapter; no async
runtime exists yet, so adopting one is deferred to the adapter issue rather
than decided here.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # the report renders itself as #27 health lines; runtime import stays lazy
    from esl_service.runtime.health import DependencyHealth

from esl_service.domain.actions import DeliveryCertainty
from esl_service.domain.failures import FailureSignal


def _require_text(value: str, name: str) -> None:
    """Reject an identifier that cannot address anything."""

    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class SourceWindow:
    """The caller-selected source interval retained for replay and audit (FR-002)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start > self.end:
            raise ValueError("source window start must not follow end")


@dataclass(frozen=True)
class StoreDirectoryEntry:
    """The verified DimStore fields needed to address one store source (FR-026)."""

    store_code: str
    org_ip: str
    org_db: str

    def __post_init__(self) -> None:
        _require_text(self.store_code, "store_code")
        _require_text(self.org_ip, "org_ip")
        _require_text(self.org_db, "org_db")


@dataclass(frozen=True)
class WarehouseProvenance:
    """Safe, persistence-ready evidence describing one warehouse snapshot."""

    instance: str
    database: str
    objects: tuple[str, ...]
    query_version: str
    source_window_start: datetime
    source_window_end: datetime
    source_watermark: datetime
    #: The isolation the rows were read under (AD-020); part of the evidence
    #: because it bounds what a replay can be expected to reproduce.
    isolation_level: str = "READ COMMITTED"

    def __post_init__(self) -> None:
        _require_text(self.instance, "instance")
        _require_text(self.isolation_level, "isolation_level")
        _require_text(self.database, "database")
        _require_text(self.query_version, "query_version")
        if not self.objects or any(not name.strip() for name in self.objects):
            raise ValueError("objects must contain non-blank source object names")
        _require_aware(self.source_window_start, "source_window_start")
        _require_aware(self.source_window_end, "source_window_end")
        if self.source_window_start > self.source_window_end:
            raise ValueError("source_window_start must not follow source_window_end")
        _require_aware(self.source_watermark, "source_watermark")


@dataclass(frozen=True)
class WarehouseReadRequest:
    """One store and one caller-approved reproducible source window."""

    store_code: str
    source_window: SourceWindow

    def __post_init__(self) -> None:
        _require_text(self.store_code, "store_code")


WarehouseRow = Mapping[str, object]


@dataclass(frozen=True)
class UnroutableStore:
    """A DimStore row the pipeline cannot address, and why (VERIFIED 2026-09-02).

    33 of 83 rows carried NULL or blank ``ORG_IP``/``ORG_DB``, including
    non-store rows such as ``Express``. The procedure skips them; so does the
    replacement, but visibly, so an operator can see which codes were never
    read (#92).
    """

    store_code: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.store_code, "store_code")
        _require_text(self.reason, "reason")


@dataclass(frozen=True)
class StoreDiscoveryResult:
    """The complete current warehouse store directory and its read evidence."""

    stores: tuple[StoreDirectoryEntry, ...]
    provenance: WarehouseProvenance
    #: Rows that named a store but not a server; never connected to.
    unroutable: tuple[UnroutableStore, ...] = ()


@dataclass(frozen=True)
class WarehouseReadResult:
    """Unfiltered warehouse facts for one store, before domain evaluation."""

    item_mappings: tuple[WarehouseRow, ...]
    campaigns: tuple[WarehouseRow, ...]
    provenance: WarehouseProvenance


@runtime_checkable
class WarehouseSourceReader(Protocol):
    """Read-only source port for the shared DBWH_8555 tier (AD-002)."""

    def discover_stores(self, source_window: SourceWindow) -> StoreDiscoveryResult:
        """Return every current store routing row from DimStore."""
        ...

    def read_store(self, request: WarehouseReadRequest) -> WarehouseReadResult:
        """Return raw mapping and campaign rows for exactly one store."""
        ...


# --- the PEPITO_HO UOM-mapping tier (#93) ------------------------------------


@dataclass(frozen=True)
class UomMappingReadRequest:
    """The item set a caller needs mappings for, and its reproducible window.

    ``ITEM_UOM_MAPPING_MST`` is central and has no store column, so the item
    set is the only honest bound. Codes are trimmed and deduplicated in order;
    an empty set is refused because an unbounded read of a central table is
    never what a caller means.
    """

    item_codes: tuple[str, ...]
    source_window: SourceWindow

    def __post_init__(self) -> None:
        cleaned: list[str] = []
        for code in self.item_codes:
            if not code.strip():
                raise ValueError("item_codes must not contain a blank code")
            if code.strip() not in cleaned:
                cleaned.append(code.strip())
        if not cleaned:
            raise ValueError("item_codes must name at least one item")
        object.__setattr__(self, "item_codes", tuple(cleaned))


@dataclass(frozen=True)
class UomMappingReadResult:
    """Unfiltered UOM-mapping rows for the requested items, before domain rules."""

    mappings: tuple[WarehouseRow, ...]
    provenance: WarehouseProvenance


@runtime_checkable
class UomMappingSourceReader(Protocol):
    """Read-only source port for the central PEPITO_HO tier (AD-002)."""

    def read_mappings(self, request: UomMappingReadRequest) -> UomMappingReadResult:
        """Return every raw mapping row for the requested item set."""
        ...


# --- the per-store iRetail tier (#92) -----------------------------------------

#: The twelve objects ``RefreshESL_New`` reads from each store's server,
#: in the order the procedure first touches them (VERIFIED, PR #80).
STORE_OBJECTS: tuple[str, ...] = (
    "dbo.ITEM_MST",
    "dbo.ITEM_DESCRIPTION",
    "dbo.CMP_HDR",
    "dbo.CMP_ORG_DTL",
    "dbo.CMP_ITEM_GRP_HDR",
    "dbo.CMP_ITEM_GRP_CND",
    "dbo.CMP_CND_MST",
    "dbo.CMP_ITEM_GRP_DTL",
    "dbo.STOCK_MASTER",
    "dbo.OFFLINE_TEMP_ITEM_MOVEMENT",
    "dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT",
    "dbo.BASIC_SP_MST",
)

_STORE_FIELDS: tuple[str, ...] = (
    "items",
    "item_descriptions",
    "campaign_headers",
    "campaign_org_details",
    "campaign_item_group_headers",
    "campaign_item_group_conditions",
    "campaign_condition_masters",
    "campaign_item_group_details",
    "stock",
    "offline_movements",
    "pos_offline_movements",
    "selling_prices",
)


@dataclass(frozen=True)
class StoreReadRequest:
    """One store, addressed from its ``DimStore`` row, and its reproducible window."""

    store: StoreDirectoryEntry
    source_window: SourceWindow


@dataclass(frozen=True)
class StoreReadResult:
    """Unfiltered rows of all twelve store objects, before domain evaluation.

    The procedure's status, validity, type, PFS, location, and price-category
    predicates are business rules and are absent here on purpose.
    """

    items: tuple[WarehouseRow, ...]
    item_descriptions: tuple[WarehouseRow, ...]
    campaign_headers: tuple[WarehouseRow, ...]
    campaign_org_details: tuple[WarehouseRow, ...]
    campaign_item_group_headers: tuple[WarehouseRow, ...]
    campaign_item_group_conditions: tuple[WarehouseRow, ...]
    campaign_condition_masters: tuple[WarehouseRow, ...]
    campaign_item_group_details: tuple[WarehouseRow, ...]
    stock: tuple[WarehouseRow, ...]
    offline_movements: tuple[WarehouseRow, ...]
    pos_offline_movements: tuple[WarehouseRow, ...]
    selling_prices: tuple[WarehouseRow, ...]
    provenance: WarehouseProvenance

    def as_mapping(self) -> dict[str, tuple[WarehouseRow, ...]]:
        """Return the rows keyed by source object name."""

        return {
            name: getattr(self, field_name)
            for name, field_name in zip(STORE_OBJECTS, _STORE_FIELDS, strict=True)
        }

    @classmethod
    def from_mapping(
        cls, rows: Mapping[str, Sequence[WarehouseRow]], provenance: WarehouseProvenance
    ) -> "StoreReadResult":
        """Build from rows keyed by object name; every object must be present."""

        missing = [name for name in STORE_OBJECTS if name not in rows]
        if missing:
            raise ValueError(f"store read is missing objects: {', '.join(missing)}")
        values = {
            field_name: tuple(rows[name])
            for name, field_name in zip(STORE_OBJECTS, _STORE_FIELDS, strict=True)
        }
        return cls(provenance=provenance, **values)


@runtime_checkable
class StoreSourceReader(Protocol):
    """Read-only source port for one store's iRetail server (AD-002)."""

    def read_store(self, request: StoreReadRequest) -> StoreReadResult:
        """Return raw rows of all twelve objects for exactly one store."""
        ...


@dataclass(frozen=True)
class StoreReadOutcome:
    """What happened for one store during a fan-out: read, failed, or skipped."""

    store_code: str
    result: StoreReadResult | None
    failure: FailureSignal | None
    skipped_reason: str | None

    def __post_init__(self) -> None:
        _require_text(self.store_code, "store_code")
        populated = sum(
            value is not None for value in (self.result, self.failure, self.skipped_reason)
        )
        if populated != 1:
            raise ValueError("an outcome is exactly one of read, failed, or skipped")

    @property
    def succeeded(self) -> bool:
        return self.result is not None

    @classmethod
    def read(cls, store_code: str, result: StoreReadResult) -> "StoreReadOutcome":
        return cls(store_code, result, None, None)

    @classmethod
    def failed(cls, store_code: str, failure: FailureSignal) -> "StoreReadOutcome":
        return cls(store_code, None, failure, None)

    @classmethod
    def skipped(cls, store_code: str, reason: str) -> "StoreReadOutcome":
        _require_text(reason, "reason")
        return cls(store_code, None, None, reason)


@dataclass(frozen=True)
class StoreFanOutReport:
    """Every store's outcome from one fan-out, ordered by store code."""

    outcomes: tuple[StoreReadOutcome, ...]

    def __post_init__(self) -> None:
        codes = [outcome.store_code for outcome in self.outcomes]
        duplicates = sorted({code for code in codes if codes.count(code) > 1})
        if duplicates:
            raise ValueError(f"store reported more than once: {', '.join(duplicates)}")
        object.__setattr__(
            self, "outcomes", tuple(sorted(self.outcomes, key=lambda o: o.store_code))
        )

    @property
    def succeeded(self) -> tuple[StoreReadOutcome, ...]:
        return tuple(o for o in self.outcomes if o.result is not None)

    @property
    def failed(self) -> tuple[StoreReadOutcome, ...]:
        return tuple(o for o in self.outcomes if o.failure is not None)

    @property
    def skipped(self) -> tuple[StoreReadOutcome, ...]:
        return tuple(o for o in self.outcomes if o.skipped_reason is not None)

    def dependency_health(self) -> tuple["DependencyHealth", ...]:
        """One #27 dependency line per store; none is required (FR-024)."""

        from esl_service.runtime.health import DependencyHealth, HealthState

        lines: list[DependencyHealth] = []
        for outcome in self.outcomes:
            if outcome.result is not None:
                state, detail = HealthState.HEALTHY, None
            elif outcome.failure is not None:
                state = HealthState.UNAVAILABLE
                detail = (
                    f"{outcome.failure.dependency.value.lower()} "
                    f"{outcome.failure.kind.value.lower()}"
                )
            else:
                state, detail = HealthState.DEGRADED, outcome.skipped_reason
            lines.append(
                DependencyHealth(
                    name=f"store-{outcome.store_code}", state=state, required=False, detail=detail
                )
            )
        return tuple(lines)


# --- the tb_ESL parity baseline, shadow mode only (#94) ------------------------


@dataclass(frozen=True)
class BaselineReadRequest:
    """One store's legacy rows and the window of the run they are compared against.

    ``tb_ESL`` is not a source (SYSTEM_ARCHITECTURE inventory, PR #80). It is
    read only under ``ESL_SHADOW_MODE`` as the baseline the computed
    canonical records are compared with (FR-021, FR-022).
    """

    store_code: str
    source_window: SourceWindow

    def __post_init__(self) -> None:
        _require_text(self.store_code, "store_code")


@dataclass(frozen=True)
class BaselineReadResult:
    """Raw ``tb_ESL`` rows for one store and the evidence of the read."""

    rows: tuple[WarehouseRow, ...]
    provenance: WarehouseProvenance


@runtime_checkable
class LegacyBaselineReader(Protocol):
    """Read-only baseline port; reachable from shadow-mode comparison only."""

    def read_baseline(self, request: BaselineReadRequest) -> BaselineReadResult:
        """Return every raw legacy row for one store."""
        ...


@dataclass(frozen=True)
class PageChange:
    """One requested label page assignment."""

    label_code: str
    page: int

    def __post_init__(self) -> None:
        _require_text(self.label_code, "label_code")


@dataclass(frozen=True)
class PageChangeReceipt:
    """What AIMS returned for an accepted page change batch."""

    response_code: str
    response_message: str
    custom_batch_id: str | None


@dataclass(frozen=True)
class AimsLabel:
    """One label as AIMS currently reports it.

    Only the fields the page-change boundary itself uses are modelled. The
    rest of the vendor read model is undiscovered and is not guessed at.
    """

    label_code: str
    store_code: str
    page: int


@dataclass(frozen=True)
class PageChangeOutcome:
    """The typed result of one page change attempt.

    The invariants keep the record honest in both directions: a confirmed
    delivery must carry vendor evidence, and anything else must carry a
    signal that ``classify`` can turn into a retry decision. An adapter can
    therefore neither claim an unevidenced success nor report a failure that
    the retry policy has no way to reason about.
    """

    certainty: DeliveryCertainty
    receipt: PageChangeReceipt | None = None
    failure: FailureSignal | None = None

    def __post_init__(self) -> None:
        if self.certainty is DeliveryCertainty.CONFIRMED:
            if self.receipt is None:
                raise ValueError("a confirmed outcome requires a receipt")
            if self.failure is not None:
                raise ValueError("a confirmed outcome cannot carry a failure")
        elif self.failure is None:
            raise ValueError(
                "an outcome that is not confirmed requires a failure signal"
            )


@runtime_checkable
class AimsPageClient(Protocol):
    """Submits label page changes to AIMS."""

    def change_pages(
        self,
        store_code: str,
        changes: Sequence[PageChange],
        idempotency_key: str,
    ) -> PageChangeOutcome:
        """Submit one batch of page changes and report what happened.

        The idempotency key is supplied by the caller so a retried attempt is
        the same attempt (FR-015). Implementations report an outcome rather
        than raising, so an interrupted call stays representable as
        ``DeliveryCertainty.UNKNOWN`` instead of collapsing into a failure.
        """
        ...


@runtime_checkable
class AimsReadModelReader(Protocol):
    """Reads the AIMS-side label state. Read-only by contract (AD-002)."""

    def fetch_labels(self, store_code: str) -> Sequence[AimsLabel]:
        """Return the labels AIMS currently reports for one store."""
        ...
