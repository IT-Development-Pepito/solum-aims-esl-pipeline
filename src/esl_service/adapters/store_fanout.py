"""Bounded fan-out across the discovered stores (#92, FR-026).

The store set is data, not configuration: it comes from ``DimStore`` at run
time (#91). The fan-out reads each store through its own reader under a
concurrency bound and reports every store individually. An unaddressable
row is skipped before any connection is attempted, a failing store becomes a
failure signal, an unexpected exception becomes a failure signal without its
text, and none of them stops the remaining stores. The report is ordered by
store code so two runs over the same directory compare line by line.
"""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from esl_service.adapters.sql_server import failure_signal
from esl_service.adapters.store import StoreReader, StoreReadError
from esl_service.application.contracts import (
    SourceWindow,
    StoreDirectoryEntry,
    StoreDiscoveryResult,
    StoreFanOutReport,
    StoreReadOutcome,
    StoreReadRequest,
    StoreSourceReader,
)
from esl_service.config import Settings
from esl_service.runtime.connectivity import InvalidStoreAddress
from esl_service.runtime.secrets import SecretProvider

#: Skip reasons, stable for the audit trail and the health report.
INVALID_STORE_ADDRESS = "INVALID_STORE_ADDRESS"
UNROUTABLE_IN_DIMSTORE = "UNROUTABLE_IN_DIMSTORE"
UNEXPECTED_READER_FAILURE = "UNEXPECTED_READER_FAILURE"

ReaderFactory = Callable[[StoreDirectoryEntry], StoreSourceReader]


class StoreFanOut:
    """Reads many stores, each in its own reader, at most ``concurrency`` at once."""

    def __init__(self, reader_factory: ReaderFactory, *, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._factory = reader_factory
        self._concurrency = concurrency

    @classmethod
    def from_settings(cls, settings: Settings, secrets: SecretProvider) -> "StoreFanOut":
        """Build readers from #78 configuration; the password stays in the bundle."""

        def build(entry: StoreDirectoryEntry) -> StoreSourceReader:
            return StoreReader.from_directory_entry(settings, secrets, entry)

        return cls(build, concurrency=settings.source_store_concurrency)

    def read_all(
        self, stores: Sequence[StoreDirectoryEntry], source_window: SourceWindow
    ) -> StoreFanOutReport:
        return self._report(stores, source_window, ())

    def read_directory(
        self, discovery: StoreDiscoveryResult, source_window: SourceWindow
    ) -> StoreFanOutReport:
        """Read every routable store #91 discovered; report the unroutable ones as skipped."""

        skipped = tuple(
            StoreReadOutcome.skipped(row.store_code, UNROUTABLE_IN_DIMSTORE)
            for row in discovery.unroutable
        )
        return self._report(discovery.stores, source_window, skipped)

    def _report(
        self,
        stores: Sequence[StoreDirectoryEntry],
        source_window: SourceWindow,
        skipped: tuple[StoreReadOutcome, ...],
    ) -> StoreFanOutReport:
        outcomes: list[StoreReadOutcome] = list(skipped)
        if stores:
            with ThreadPoolExecutor(
                max_workers=self._concurrency, thread_name_prefix="esl-store"
            ) as pool:
                outcomes.extend(pool.map(lambda s: self._read_one(s, source_window), stores))
        return StoreFanOutReport(tuple(outcomes))

    def _read_one(self, store: StoreDirectoryEntry, source_window: SourceWindow) -> StoreReadOutcome:
        try:
            reader = self._factory(store)
        except InvalidStoreAddress:
            return StoreReadOutcome.skipped(store.store_code, INVALID_STORE_ADDRESS)
        try:
            result = reader.read_store(StoreReadRequest(store, source_window))
        except StoreReadError as error:
            return StoreReadOutcome.failed(store.store_code, error.signal)
        except Exception as error:  # noqa: BLE001 - one store must not stop the fan-out
            return StoreReadOutcome.failed(store.store_code, failure_signal(error))
        return StoreReadOutcome.read(store.store_code, result)
