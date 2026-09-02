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
from typing import Protocol, runtime_checkable

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

    def __post_init__(self) -> None:
        _require_text(self.instance, "instance")
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
class StoreDiscoveryResult:
    """The complete current warehouse store directory and its read evidence."""

    stores: tuple[StoreDirectoryEntry, ...]
    provenance: WarehouseProvenance


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
