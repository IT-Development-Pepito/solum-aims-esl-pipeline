"""Read-only PEPITO_HO adapter (#93, FR-001, FR-002, FR-025).

The procedure reads ``ITEM_UOM_MAPPING_MST`` with three predicates
(``IUM_UOM_MAP_STATUS = 'O'``, ``IUM_MAIN_ITM_BARCODE = 1``,
``IUM_SALES_UOM_FLAG = 1``) joined on item code. Those predicates are the
business rule that picks the selling UOM and barcode, so they stay in the
domain (#36): the adapter returns every mapping row for the requested item
set, raw, in one transaction, with the same provenance shape as #91.
"""

import inspect
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import pepito_ho as pepito_ho_module
from esl_service.adapters.pepito_ho import (
    UOM_MAPPING_CHUNK_SIZE,
    UOM_MAPPING_QUERY_VERSION,
    PepitoHoReader,
    PepitoHoReadError,
    SqlAlchemyPepitoHoExecutor,
    UomMappingRows,
)
from esl_service.application.contracts import (
    SourceWindow,
    UomMappingReadRequest,
    UomMappingSourceReader,
)
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.secrets import SOURCE_SQL_PASSWORD_KEY

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
DATABASE_NOW = datetime(2026, 9, 2, 2, 0, 1, tzinfo=UTC)

SCHEMA = (
    "SELECT IUM_ITM_CD, IUM_LEAST_UOM_CD, IUM_BAR_ITM_CD, IUM_UOM_MAP_STATUS, "
    "IUM_MAIN_ITM_BARCODE, IUM_SALES_UOM_FLAG FROM dbo.ITEM_UOM_MAPPING_MST WHERE 1 = 0"
)
READ_TIME = "SELECT SYSUTCDATETIME() AS source_watermark"


def mappings_sql(count: int) -> str:
    placeholders = ", ".join(f":item_{index}" for index in range(count))
    return f"SELECT * FROM dbo.ITEM_UOM_MAPPING_MST WHERE IUM_ITM_CD IN ({placeholders})"


RAW_ROWS: tuple[Mapping[str, object], ...] = (
    {
        "IUM_ITM_CD": "SKU-1",
        "IUM_LEAST_UOM_CD": "PCS",
        "IUM_BAR_ITM_CD": "899000000001",
        "IUM_UOM_MAP_STATUS": "O",
        "IUM_MAIN_ITM_BARCODE": 1,
        "IUM_SALES_UOM_FLAG": 1,
    },
    {
        "IUM_ITM_CD": "SKU-1",
        "IUM_LEAST_UOM_CD": "CTN",
        "IUM_BAR_ITM_CD": "899000000002",
        "IUM_UOM_MAP_STATUS": "C",  # closed: the domain, not the adapter, drops it
        "IUM_MAIN_ITM_BARCODE": 0,
        "IUM_SALES_UOM_FLAG": 0,
    },
)


class FakeExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.transactions = 0
        self.requested: list[tuple[str, ...]] = []

    def read_mappings(self, item_codes: Sequence[str]) -> UomMappingRows:
        self.transactions += 1
        self.requested.append(tuple(item_codes))
        if self.error is not None:
            raise self.error
        return UomMappingRows(RAW_ROWS, DATABASE_NOW)


class RecordingResult:
    def __init__(
        self, rows: Sequence[Mapping[str, object]] = (), scalar: object | None = None
    ) -> None:
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> "RecordingResult":
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)

    def scalar_one(self) -> object:
        return self._scalar


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.begins = 0

    def begin(self) -> Any:
        self.begins += 1
        return nullcontext()

    def execute(
        self, statement: object, parameters: Mapping[str, object] | None = None
    ) -> RecordingResult:
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        if sql == READ_TIME:
            return RecordingResult(scalar=DATABASE_NOW)
        if sql.startswith("SELECT * FROM dbo.ITEM_UOM_MAPPING_MST"):
            return RecordingResult(({"IUM_ITM_CD": "SKU-1"},))
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


def reader(executor: FakeExecutor) -> PepitoHoReader:
    return PepitoHoReader(executor, instance="192.168.85.18", database="PEPITO_HO")


def request(*codes: str) -> UomMappingReadRequest:
    return UomMappingReadRequest(codes or ("SKU-1",), SourceWindow(START, END))


# --- raw rows, one transaction, honest provenance --------------------------------


def test_returns_every_mapping_row_raw_including_closed_and_non_sales_ones() -> None:
    """No status, barcode, or sales-flag predicate is applied here (#36 owns them)."""

    executor = FakeExecutor()

    result = reader(executor).read_mappings(request("SKU-1"))

    assert len(result.mappings) == 2
    assert {row["IUM_UOM_MAP_STATUS"] for row in result.mappings} == {"O", "C"}
    assert executor.transactions == 1
    assert executor.requested == [("SKU-1",)]


def test_provenance_names_the_tier_the_object_the_window_and_the_watermark() -> None:
    result = reader(FakeExecutor()).read_mappings(request("SKU-1"))

    provenance = result.provenance
    assert provenance.instance == "192.168.85.18"
    assert provenance.database == "PEPITO_HO"
    assert provenance.objects == ("dbo.ITEM_UOM_MAPPING_MST",)
    assert provenance.query_version == UOM_MAPPING_QUERY_VERSION
    assert provenance.source_window_start == START
    assert provenance.source_window_end == END
    assert provenance.source_watermark == DATABASE_NOW
    assert provenance.isolation_level == "READ COMMITTED"


def test_provenance_records_the_isolation_the_reader_was_built_with() -> None:
    snapshot_reader = PepitoHoReader(
        FakeExecutor(), instance="192.168.85.18", database="PEPITO_HO", isolation_level="SNAPSHOT"
    )

    assert snapshot_reader.read_mappings(request("SKU-1")).provenance.isolation_level == "SNAPSHOT"


# --- the concrete executor ------------------------------------------------------------


def test_concrete_executor_emits_only_the_exact_approved_selects_in_one_transaction() -> None:
    engine = RecordingEngine()
    executor = SqlAlchemyPepitoHoExecutor(cast(Engine, engine))

    executor.read_mappings(("SKU-1", "SKU-2"))

    assert engine.connection.begins == 1
    assert engine.connection.calls == [
        (SCHEMA, {}),
        (READ_TIME, {}),
        (mappings_sql(2), {"item_0": "SKU-1", "item_1": "SKU-2"}),
    ]
    assert all(sql.startswith("SELECT ") for sql, _ in engine.connection.calls)


def test_concrete_executor_chunks_a_large_item_set_but_keeps_one_transaction() -> None:
    engine = RecordingEngine()
    executor = SqlAlchemyPepitoHoExecutor(cast(Engine, engine))
    codes = tuple(f"SKU-{index}" for index in range(UOM_MAPPING_CHUNK_SIZE + 1))

    rows = executor.read_mappings(codes)

    selects = [call for call in engine.connection.calls if call[0].startswith("SELECT * FROM")]
    assert len(selects) == 2
    assert len(selects[0][1]) == UOM_MAPPING_CHUNK_SIZE
    assert len(selects[1][1]) == 1
    assert engine.connection.begins == 1
    assert len(rows.rows) == 2  # one fake row per chunk


def test_no_predicate_beyond_the_item_set_appears_in_the_sql() -> None:
    engine = RecordingEngine()
    SqlAlchemyPepitoHoExecutor(cast(Engine, engine)).read_mappings(("SKU-1",))

    (data_select,) = [sql for sql, _ in engine.connection.calls if sql.startswith("SELECT * FROM")]
    for business_column in ("IUM_UOM_MAP_STATUS", "IUM_MAIN_ITM_BARCODE", "IUM_SALES_UOM_FLAG"):
        assert business_column not in data_select


def test_production_facing_api_accepts_no_caller_supplied_sql() -> None:
    assert pepito_ho_module.__all__ == [
        "UOM_MAPPING_QUERY_VERSION",
        "PepitoHoReadError",
        "PepitoHoReader",
    ]
    executor_methods = {
        name
        for name, _ in inspect.getmembers(SqlAlchemyPepitoHoExecutor, inspect.isfunction)
        if not name.startswith("_")
    }
    assert executor_methods == {"read_mappings"}
    parameters = inspect.signature(SqlAlchemyPepitoHoExecutor.read_mappings).parameters
    assert not {"sql", "statement", "query"}.intersection(parameters)


def test_reader_satisfies_the_port_and_exposes_no_write_api() -> None:
    uom = reader(FakeExecutor())

    assert isinstance(uom, UomMappingSourceReader)
    public = [
        name
        for name, _ in inspect.getmembers(PepitoHoReader, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public
    assert not [
        name
        for name in public
        if name.casefold().startswith(
            ("insert", "update", "delete", "write", "set_", "create", "drop", "truncate")
        )
    ]


# --- factory and failures ----------------------------------------------------------------


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://state@localhost/esl",
        "internal_host": "127.0.0.1",
        "source_sql_username": "esl_reader",
        "source_sql_driver": "ODBC Driver 18 for SQL Server",
        "source_pepito_ho_host": "192.168.85.18",
        "source_pepito_ho_database": "PEPITO_HO",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_factory_uses_the_pepito_ho_settings_and_the_shared_source_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[URL, str]] = []

    def capture_engine(url: URL, *, isolation_level: str) -> Engine:
        captured.append((url, isolation_level))
        return cast(Engine, object())

    monkeypatch.setattr(pepito_ho_module, "create_read_only_engine", capture_engine)
    secrets = FakeSecrets()

    built = PepitoHoReader.from_settings(settings(), secrets)

    assert secrets.requested == [SOURCE_SQL_PASSWORD_KEY]
    ((url, isolation_level),) = captured
    assert url.host == "192.168.85.18"
    assert url.database == "PEPITO_HO"
    assert url.username == "esl_reader"
    assert url.query["driver"] == "ODBC Driver 18 for SQL Server"
    assert isolation_level == "READ COMMITTED"
    assert "test-only-password" not in repr(built)


def test_factory_passes_the_configured_isolation_level_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_engine(url: URL, *, isolation_level: str) -> Engine:
        captured.append(isolation_level)
        return cast(Engine, object())

    monkeypatch.setattr(pepito_ho_module, "create_read_only_engine", capture_engine)

    built = PepitoHoReader.from_settings(
        settings(source_sql_isolation_level="SNAPSHOT"), FakeSecrets()
    )

    assert captured == ["SNAPSHOT"]
    assert built.read_mappings.__self__ is built  # the reader is usable as built


def test_factory_refuses_an_unconfigured_tier() -> None:
    with pytest.raises(ValueError, match="pepito-ho"):
        PepitoHoReader.from_settings(settings(source_pepito_ho_host=""), FakeSecrets())


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (
            DriverError("08001", "tcp://192.168.85.18:1433?password=secret"),
            FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE),
        ),
        (
            DriverError("28000", "login failed; password=secret"),
            FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED),
        ),
        (
            DriverError("IM002", "driver missing; password=secret"),
            FailureSignal(DependencyKind.CONFIGURATION, FailureKind.MALFORMED),
        ),
        (
            DriverError("42S02", "invalid object dbo.Secret; password=secret"),
            FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED),
        ),
    ],
)
def test_failures_map_to_safe_existing_signals(
    driver_error: DriverError, expected: FailureSignal
) -> None:
    with pytest.raises(PepitoHoReadError) as raised:
        reader(FakeExecutor(error=driver_error)).read_mappings(request("SKU-1"))

    assert raised.value.signal == expected
    assert "secret" not in str(raised.value).casefold()
    assert "://" not in str(raised.value)


def test_a_non_datetime_watermark_is_malformed_source_data() -> None:
    class BadClock(FakeExecutor):
        def read_mappings(self, item_codes: Sequence[str]) -> UomMappingRows:
            return UomMappingRows(RAW_ROWS, cast(datetime, "not a datetime"))

    with pytest.raises(PepitoHoReadError) as raised:
        reader(BadClock()).read_mappings(request("SKU-1"))

    assert raised.value.signal == FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)
