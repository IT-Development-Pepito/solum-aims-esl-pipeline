"""Read-only per-store iRetail adapter (#92, FR-001/002/025/026).

A store server is addressed from its ``DimStore`` row through the #78
``store_target`` validation and read as a whole in one transaction. The
procedure applies its business predicates in SQL (``ITM_STATUS = 'O'``,
``CMP_STATUS = 'A'``, ``CIGD_STATUS = 'O'``, validity dates, ``CMP_TYPE IN
(0,1,3)``, the PFS exclusion, ``LOC_CD = '001'``, ``STOCK_UPDATED_FLAG IS
NULL``, ``BSP_PRICE_CATG = '001'``, ``BSP_STATUS = 'A'``); every one of them
is a domain rule, so the only predicate here is the store code.
"""

import inspect
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import store as store_module
from esl_service.adapters.store import (
    STORE_QUERY_VERSION,
    STORE_SCHEMA_PROBES,
    STORE_SELECTS,
    SqlAlchemyStoreExecutor,
    StoreReader,
    StoreReadError,
    StoreRows,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadRequest,
    StoreSourceReader,
)
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.connectivity import InvalidStoreAddress
from esl_service.runtime.secrets import SOURCE_SQL_PASSWORD_KEY

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
DATABASE_NOW = datetime(2026, 9, 2, 2, 0, 1, tzinfo=UTC)
READ_TIME = "SELECT SYSUTCDATETIME() AS source_watermark"
STORE = StoreDirectoryEntry("084", "10.0.0.84", "STORE_084")

BUSINESS_TOKENS = (
    "ITM_STATUS",
    "CMP_STATUS",
    "CIGD_STATUS",
    "CMP_TYPE",
    "PFS",
    "CMP_FROM_DATE",
    "CMP_TO_DATE",
    "LOC_CD",
    "STOCK_UPDATED_FLAG",
    "BSP_PRICE_CATG",
    "BSP_STATUS",
    "LAST_UPDATED_DATE",
)


def raw_rows() -> dict[str, tuple[Mapping[str, object], ...]]:
    return {
        name: ({"OBJECT": name, "ROW": 1},) for name in STORE_OBJECTS
    } | {
        "dbo.ITEM_MST": ({"ITM_CD": "SKU-1", "ITM_STATUS": "O"}, {"ITM_CD": "SKU-2", "ITM_STATUS": "C"}),
        "dbo.BASIC_SP_MST": (
            {"BSP_ITEM_CD": "SKU-1", "BSP_PRICE_CATG": "001", "BSP_STATUS": "A"},
            {"BSP_ITEM_CD": "SKU-1", "BSP_PRICE_CATG": "002", "BSP_STATUS": "X"},
        ),
    }


class FakeExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.transactions = 0
        self.requested: list[str] = []

    def read_store(self, store_code: str) -> StoreRows:
        self.transactions += 1
        self.requested.append(store_code)
        if self.error is not None:
            raise self.error
        return StoreRows(raw_rows(), DATABASE_NOW)


class RecordingResult:
    def __init__(self, rows: Sequence[Mapping[str, object]] = (), scalar: object | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> "RecordingResult":
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)

    def scalar_one(self) -> object:
        return self._scalar


class FakeDbapiConnection:
    timeout = 0


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.begins = 0
        self.connection = FakeDbapiConnection()

    def begin(self) -> Any:
        self.begins += 1
        return nullcontext()

    def execute(self, statement: object, parameters: Mapping[str, object] | None = None) -> RecordingResult:
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        if sql == READ_TIME:
            return RecordingResult(scalar=DATABASE_NOW)
        if sql in STORE_SELECTS.values():
            return RecordingResult(({"ROW": 1},))
        return RecordingResult()


class RecordingEngine:
    def __init__(self) -> None:
        self.connection = RecordingConnection()

    def connect(self) -> Any:
        return nullcontext(self.connection)


class DriverError(Exception):
    pass


class FakeSecrets:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, key: str) -> str:
        self.requested.append(key)
        return "test-only-password"


def reader(executor: FakeExecutor) -> StoreReader:
    return StoreReader(executor, store=STORE)


def request() -> StoreReadRequest:
    return StoreReadRequest(STORE, SourceWindow(START, END))


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://state@localhost/esl",
        "internal_host": "127.0.0.1",
        "source_sql_username": "esl_reader",
        "source_sql_driver": "ODBC Driver 18 for SQL Server",
    }
    values.update(overrides)
    return Settings.model_validate(values)


# --- raw rows for all twelve objects, one transaction ----------------------------


def test_returns_every_object_raw_including_rows_the_procedure_would_filter() -> None:
    executor = FakeExecutor()

    result = reader(executor).read_store(request())

    assert executor.transactions == 1 and executor.requested == ["084"]
    assert {row["ITM_STATUS"] for row in result.items} == {"O", "C"}
    assert {row["BSP_PRICE_CATG"] for row in result.selling_prices} == {"001", "002"}
    assert all(len(rows) >= 1 for rows in result.as_mapping().values())


def test_provenance_names_the_store_server_and_all_twelve_objects() -> None:
    provenance = reader(FakeExecutor()).read_store(request()).provenance

    assert provenance.instance == "10.0.0.84"
    assert provenance.database == "STORE_084"
    assert provenance.objects == STORE_OBJECTS
    assert provenance.query_version == STORE_QUERY_VERSION
    assert provenance.source_window_start == START
    assert provenance.source_window_end == END
    assert provenance.source_watermark == DATABASE_NOW
    assert provenance.isolation_level == "READ COMMITTED"


def test_a_request_for_a_different_store_than_the_reader_is_refused() -> None:
    other = StoreReadRequest(StoreDirectoryEntry("075", "10.0.0.75", "STORE_075"), SourceWindow(START, END))

    with pytest.raises(ValueError, match="075"):
        reader(FakeExecutor()).read_store(other)


# --- the SQL is scope-only ------------------------------------------------------------


def test_the_twelve_selects_are_bounded_by_store_code_only() -> None:
    assert set(STORE_SELECTS) == set(STORE_OBJECTS)
    for name, sql in STORE_SELECTS.items():
        assert sql.startswith("SELECT "), name
        for token in BUSINESS_TOKENS:
            assert token not in sql.upper(), (name, token)
        params = {p for p in sql.split() if p.startswith(":")}
        assert params <= {":store_code"}, name


def test_concrete_executor_probes_schema_reads_the_clock_then_selects_in_one_transaction() -> None:
    engine = RecordingEngine()

    rows = SqlAlchemyStoreExecutor(cast(Engine, engine), statement_timeout_seconds=45).read_store("084")

    assert engine.connection.begins == 1
    calls = engine.connection.calls
    assert [sql for sql, _ in calls[: len(STORE_SCHEMA_PROBES)]] == list(STORE_SCHEMA_PROBES.values())
    assert calls[len(STORE_SCHEMA_PROBES)] == (READ_TIME, {})
    data = calls[len(STORE_SCHEMA_PROBES) + 1 :]
    assert [sql for sql, _ in data] == list(STORE_SELECTS.values())
    assert all(params in ({}, {"store_code": "084"}) for _, params in data)
    assert set(rows.rows) == set(STORE_OBJECTS)
    assert engine.connection.connection.timeout == 45


def test_production_facing_api_accepts_no_caller_supplied_sql() -> None:
    assert store_module.__all__ == ["STORE_QUERY_VERSION", "StoreReadError", "StoreReader"]
    methods = {
        name
        for name, _ in inspect.getmembers(SqlAlchemyStoreExecutor, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == {"read_store"}
    assert not {"sql", "statement", "query"}.intersection(
        inspect.signature(SqlAlchemyStoreExecutor.read_store).parameters
    )


def test_reader_satisfies_the_port_and_exposes_no_write_api() -> None:
    store_reader = reader(FakeExecutor())

    assert isinstance(store_reader, StoreSourceReader)
    public = [n for n, _ in inspect.getmembers(StoreReader, inspect.isfunction) if not n.startswith("_")]
    assert public
    assert not [
        n
        for n in public
        if n.casefold().startswith(("insert", "update", "delete", "write", "set_", "create", "drop", "truncate"))
    ]


# --- addressing a store from its DimStore row ----------------------------------------


def test_factory_validates_the_address_and_uses_the_shared_source_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[URL, str]] = []

    def capture_engine(url: URL, *, isolation_level: str) -> Engine:
        captured.append((url, isolation_level))
        return cast(Engine, object())

    monkeypatch.setattr(store_module, "create_read_only_engine", capture_engine)
    secrets = FakeSecrets()

    built = StoreReader.from_directory_entry(settings(), secrets, STORE)

    assert secrets.requested == [SOURCE_SQL_PASSWORD_KEY]
    ((url, level),) = captured
    assert url.host == "10.0.0.84" and url.port is None
    assert url.database == "STORE_084"
    assert url.username == "esl_reader"
    assert level == "READ COMMITTED"
    assert "test-only-password" not in repr(built)


@pytest.mark.parametrize(
    "entry",
    [
        StoreDirectoryEntry("084", "10.0.0.84:1433", "STORE_084"),
        StoreDirectoryEntry("084", "10.0.0.84;Encrypt=no", "STORE_084"),
        StoreDirectoryEntry("084", "10.0.0.84", "STORE_084;DROP TABLE x"),
    ],
)
def test_factory_refuses_an_address_that_is_not_a_plain_host_or_identifier(
    entry: StoreDirectoryEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(url: URL, *, isolation_level: str) -> Engine:
        raise AssertionError("no engine may be created for an invalid address")

    monkeypatch.setattr(store_module, "create_read_only_engine", never)

    with pytest.raises(InvalidStoreAddress):
        StoreReader.from_directory_entry(settings(), FakeSecrets(), entry)


def test_factory_passes_the_configured_isolation_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    levels: list[str] = []

    def capture_engine(url: URL, *, isolation_level: str) -> Engine:
        levels.append(isolation_level)
        return cast(Engine, object())

    monkeypatch.setattr(store_module, "create_read_only_engine", capture_engine)

    built = StoreReader.from_directory_entry(
        settings(source_sql_isolation_level="SNAPSHOT", source_store_read_timeout_seconds=7),
        FakeSecrets(),
        STORE,
    )

    assert levels == ["SNAPSHOT"]
    assert built.statement_timeout_seconds == 7


# --- failures ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (DriverError("08001", "tcp://10.0.0.84:1433?password=secret"), FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)),
        (DriverError("28000", "login failed; password=secret"), FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED)),
        (DriverError("IM002", "driver missing; password=secret"), FailureSignal(DependencyKind.CONFIGURATION, FailureKind.MALFORMED)),
        (DriverError("42S02", "invalid object dbo.Secret; password=secret"), FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)),
        (DriverError("HYT00", "query timeout expired; password=secret"), FailureSignal(DependencyKind.SQL_SERVER, FailureKind.TIMEOUT)),
    ],
)
def test_failures_map_to_safe_existing_signals(driver_error: DriverError, expected: FailureSignal) -> None:
    with pytest.raises(StoreReadError) as raised:
        reader(FakeExecutor(error=driver_error)).read_store(request())

    assert raised.value.signal == expected
    assert raised.value.store_code == "084"
    assert "secret" not in str(raised.value).casefold()
    assert "://" not in str(raised.value)
