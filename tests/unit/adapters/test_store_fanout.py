"""Bounded fan-out across the discovered stores (#92, FR-026).

The store set comes from ``DimStore`` at run time (#91). The fan-out reads
each store through its own reader under a concurrency bound, and reports
each store individually: an unaddressable store is skipped without a
connection attempt, a failing store is a failure signal, and neither stops
the others. The report is ordered by store code so it is reproducible.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from esl_service.adapters.store import StoreReadError
from esl_service.adapters.store_fanout import (
    INVALID_STORE_ADDRESS,
    UNEXPECTED_READER_FAILURE,
    StoreFanOut,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadRequest,
    StoreReadResult,
    WarehouseProvenance,
)
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.connectivity import InvalidStoreAddress

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
WINDOW = SourceWindow(START, END)

STORES = (
    StoreDirectoryEntry("084", "10.0.0.84", "STORE_084"),
    StoreDirectoryEntry("075", "10.0.0.75", "STORE_075"),
    StoreDirectoryEntry("090", "10.0.0.90", "STORE_090"),
)


def result_for(store: StoreDirectoryEntry) -> StoreReadResult:
    empty: dict[str, tuple[dict[str, object], ...]] = {name: () for name in STORE_OBJECTS}
    return StoreReadResult.from_mapping(
        empty,
        WarehouseProvenance(
            instance=store.org_ip,
            database=store.org_db,
            objects=STORE_OBJECTS,
            query_version="store-current-state-v1",
            source_window_start=START,
            source_window_end=END,
            source_watermark=END,
        ),
    )


class FakeReader:
    def __init__(self, store: StoreDirectoryEntry, tracker: "Tracker", behaviour: str) -> None:
        self.store = store
        self.tracker = tracker
        self.behaviour = behaviour

    def read_store(self, request: StoreReadRequest) -> StoreReadResult:
        with self.tracker.enter():
            time.sleep(0.02)
            if self.behaviour == "fail":
                raise StoreReadError(
                    request.store.store_code,
                    FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE),
                )
            if self.behaviour == "crash":
                raise RuntimeError("tcp://10.0.0.75?password=secret")
            return result_for(request.store)


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self) -> "Tracker":
        return self

    def __enter__(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def __exit__(self, *_: object) -> None:
        with self.lock:
            self.active -= 1


def factory(
    tracker: Tracker, behaviours: dict[str, str] | None = None
) -> Callable[[StoreDirectoryEntry], FakeReader]:
    behaviours = behaviours or {}

    def build(store: StoreDirectoryEntry) -> FakeReader:
        if store.org_ip.endswith(":bad"):
            raise InvalidStoreAddress(f"store {store.store_code!r} address must be a bare IP address or hostname")
        return FakeReader(store, tracker, behaviours.get(store.store_code, "ok"))

    return build


def test_every_store_is_read_and_the_report_is_ordered_by_store_code() -> None:
    tracker = Tracker()

    report = StoreFanOut(factory(tracker), concurrency=2).read_all(STORES, WINDOW)

    assert [o.store_code for o in report.outcomes] == ["075", "084", "090"]
    assert all(o.succeeded for o in report.outcomes)
    assert report.succeeded[0].result is not None
    assert report.succeeded[0].result.provenance.database == "STORE_075"


def test_the_concurrency_bound_is_honoured() -> None:
    tracker = Tracker()
    many = tuple(StoreDirectoryEntry(f"{n:03d}", f"10.0.0.{n}", f"STORE_{n:03d}") for n in range(1, 9))

    StoreFanOut(factory(tracker), concurrency=3).read_all(many, WINDOW)

    assert tracker.peak <= 3
    assert tracker.peak >= 2  # it did run in parallel


def test_one_failing_store_is_reported_and_the_others_still_complete() -> None:
    tracker = Tracker()

    report = StoreFanOut(factory(tracker, {"084": "fail"}), concurrency=2).read_all(STORES, WINDOW)

    by_code = {o.store_code: o for o in report.outcomes}
    assert by_code["084"].failure == FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)
    assert by_code["075"].succeeded and by_code["090"].succeeded


def test_an_unexpected_exception_becomes_a_failure_without_its_text() -> None:
    tracker = Tracker()

    report = StoreFanOut(factory(tracker, {"075": "crash"}), concurrency=2).read_all(STORES, WINDOW)

    (failed,) = report.failed
    assert failed.store_code == "075"
    assert failed.failure is not None
    assert failed.skipped_reason is None
    assert "secret" not in repr(report) and "://" not in repr(report)
    assert UNEXPECTED_READER_FAILURE  # the reason code exists for the audit trail


def test_an_unaddressable_store_is_skipped_without_a_connection_attempt() -> None:
    tracker = Tracker()
    bad = StoreDirectoryEntry("086", "10.0.0.86:bad", "STORE_086")

    report = StoreFanOut(factory(tracker), concurrency=2).read_all((*STORES, bad), WINDOW)

    (skipped,) = report.skipped
    assert skipped.store_code == "086"
    assert skipped.skipped_reason == INVALID_STORE_ADDRESS
    assert len(report.succeeded) == 3


def test_reading_a_discovery_result_reports_unroutable_rows_as_skipped() -> None:
    """The fan-out consumes #91's directory whole: routable rows are read,
    rows DimStore could not route are skipped with a stable reason, and the
    report still lists every store code the directory named."""

    from esl_service.adapters.store_fanout import UNROUTABLE_IN_DIMSTORE
    from esl_service.application.contracts import StoreDiscoveryResult, UnroutableStore

    discovery = StoreDiscoveryResult(
        stores=STORES[:2],
        provenance=result_for(STORES[0]).provenance,
        unroutable=(UnroutableStore("001", "ORG_IP and ORG_DB are missing"),),
    )

    report = StoreFanOut(factory(Tracker()), concurrency=2).read_directory(discovery, WINDOW)

    assert [o.store_code for o in report.outcomes] == ["001", "075", "084"]
    (skipped,) = report.skipped
    assert skipped.store_code == "001" and skipped.skipped_reason == UNROUTABLE_IN_DIMSTORE
    assert len(report.succeeded) == 2


def test_an_empty_store_set_is_an_empty_report() -> None:
    assert StoreFanOut(factory(Tracker()), concurrency=2).read_all((), WINDOW).outcomes == ()


@pytest.mark.parametrize("concurrency", [0, -1])
def test_a_non_positive_concurrency_is_refused(concurrency: int) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        StoreFanOut(factory(Tracker()), concurrency=concurrency)
