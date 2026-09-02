"""Read-only DBWH_8555 adapter (#91, FR-001, FR-002, FR-025, FR-026)."""

import inspect
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import sql_server as sql_server_module
from esl_service.adapters import warehouse as warehouse_module
from esl_service.adapters.warehouse import (
    WAREHOUSE_QUERY_VERSION,
    SqlAlchemyWarehouseExecutor,
    WarehouseDirectoryRows,
    WarehouseReader,
    WarehouseReadError,
    WarehouseStoreRows,
    build_read_only_url,
    create_warehouse_engine,
)
from esl_service.application.contracts import (
    SourceWindow,
    WarehouseReadRequest,
    WarehouseSourceReader,
)
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.secrets import SOURCE_SQL_PASSWORD_KEY

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
DATABASE_NOW = datetime(2026, 9, 2, 2, 0, 1, tzinfo=UTC)

STORE_SCHEMA = "SELECT ORG_CD, ORG_IP, ORG_DB FROM dbo.DimStore WHERE 1 = 0"
MAPPING_SCHEMA = "SELECT OID_ORG_CD FROM dbo.DimItemMapping WHERE 1 = 0"
CAMPAIGN_SCHEMA = "SELECT FOR_ORGANIZATION FROM dbo.FactCampaign WHERE 1 = 0"
READ_TIME = "SELECT SYSUTCDATETIME() AS source_watermark"
STORES = "SELECT ORG_CD, ORG_IP, ORG_DB FROM dbo.DimStore ORDER BY ORG_CD"
MAPPINGS = "SELECT * FROM dbo.DimItemMapping WHERE OID_ORG_CD = :store_code"
CAMPAIGNS = "SELECT * FROM dbo.FactCampaign WHERE FOR_ORGANIZATION = :store_code"


class FakeExecutor:
    def __init__(
        self,
        *,
        directory_rows: Sequence[Mapping[str, object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.directory_rows = directory_rows or (
            {"ORG_CD": "084", "ORG_IP": "10.0.0.84", "ORG_DB": "STORE_084"},
            {"ORG_CD": "085", "ORG_IP": "10.0.0.85", "ORG_DB": "STORE_085"},
        )
        self.error = error
        self.transactions = 0

    def discover_stores(self) -> WarehouseDirectoryRows:
        self.transactions += 1
        if self.error is not None:
            raise self.error
        return WarehouseDirectoryRows(tuple(self.directory_rows), DATABASE_NOW)

    def read_store(self, store_code: str) -> WarehouseStoreRows:
        self.transactions += 1
        if self.error is not None:
            raise self.error
        assert store_code == "084"
        return WarehouseStoreRows(
            item_mappings=(
                {"OID_ORG_CD": "084", "OID_ITM_CD": "SKU-1", "OID_ITM_STATUS": "C"},
            ),
            campaigns=(
                {
                    "FOR_ORGANIZATION": "084",
                    "CAMPAIGN_STATUS": "INACTIVE",
                    "PFS": "Y",
                    "CAMPAIGN_TYPE": "UNSUPPORTED-EVIDENCE",
                },
            ),
            source_watermark=DATABASE_NOW,
        )


class RecordingResult:
    def __init__(
        self,
        rows: Sequence[Mapping[str, object]] = (),
        scalar: object | None = None,
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

    def begin(self) -> Any:
        return nullcontext()

    def execute(
        self, statement: object, parameters: Mapping[str, object] | None = None
    ) -> RecordingResult:
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        if sql == READ_TIME:
            return RecordingResult(scalar=DATABASE_NOW)
        if sql == STORES:
            return RecordingResult(
                ({"ORG_CD": "084", "ORG_IP": "10.0.0.84", "ORG_DB": "STORE_084"},)
            )
        if sql == MAPPINGS:
            return RecordingResult(({"OID_ORG_CD": "084"},))
        if sql == CAMPAIGNS:
            return RecordingResult(({"FOR_ORGANIZATION": "084"},))
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


def reader(executor: FakeExecutor) -> WarehouseReader:
    return WarehouseReader(
        executor,
        instance="warehouse.internal",
        database="DBWH_8555",
    )


def test_discovers_every_store_dynamically_and_records_provenance() -> None:
    executor = FakeExecutor()

    result = reader(executor).discover_stores(SourceWindow(START, END))

    assert [store.store_code for store in result.stores] == ["084", "085"]
    assert result.stores[0].org_ip == "10.0.0.84"
    assert result.provenance.instance == "warehouse.internal"
    assert result.provenance.database == "DBWH_8555"
    assert result.provenance.objects == ("dbo.DimStore",)
    assert result.provenance.isolation_level == "READ COMMITTED"
    assert result.provenance.query_version == WAREHOUSE_QUERY_VERSION
    assert result.provenance.source_watermark == DATABASE_NOW
    assert result.provenance.source_window_start == START
    assert result.provenance.source_window_end == END
    assert executor.transactions == 1


def test_reads_raw_rows_for_one_store_in_one_transaction() -> None:
    executor = FakeExecutor()
    request = WarehouseReadRequest("084", SourceWindow(START, END))

    result = reader(executor).read_store(request)

    assert result.item_mappings[0]["OID_ITM_STATUS"] == "C"
    assert result.campaigns[0]["CAMPAIGN_STATUS"] == "INACTIVE"
    assert result.campaigns[0]["PFS"] == "Y"
    assert result.provenance.source_window_start == START
    assert result.provenance.source_window_end == END
    assert result.provenance.source_watermark == DATABASE_NOW
    assert result.provenance.objects == (
        "dbo.DimItemMapping",
        "dbo.FactCampaign",
    )
    assert executor.transactions == 1


def test_concrete_executor_emits_only_the_exact_approved_selects() -> None:
    engine = RecordingEngine()
    executor = SqlAlchemyWarehouseExecutor(cast(Engine, engine))

    executor.discover_stores()
    executor.read_store("084")

    assert engine.connection.calls == [
        (STORE_SCHEMA, {}),
        (READ_TIME, {}),
        (STORES, {}),
        (MAPPING_SCHEMA, {}),
        (CAMPAIGN_SCHEMA, {}),
        (READ_TIME, {}),
        (MAPPINGS, {"store_code": "084"}),
        (CAMPAIGNS, {"store_code": "084"}),
    ]
    assert all(sql.startswith("SELECT ") for sql, _ in engine.connection.calls)


def test_production_facing_api_accepts_no_caller_supplied_sql() -> None:
    assert warehouse_module.__all__ == [
        "WAREHOUSE_QUERY_VERSION",
        "WarehouseReadError",
        "WarehouseReader",
    ]
    assert not hasattr(warehouse_module, "SqlReadSession")
    executor_methods = {
        name
        for name, method in inspect.getmembers(
            SqlAlchemyWarehouseExecutor, inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert executor_methods == {"discover_stores", "read_store"}
    for method_name in executor_methods:
        parameters = inspect.signature(
            getattr(SqlAlchemyWarehouseExecutor, method_name)
        ).parameters
        assert not {"sql", "statement", "query"}.intersection(parameters)


def test_reader_satisfies_the_application_port_and_exposes_no_write_api() -> None:
    warehouse = reader(FakeExecutor())

    assert isinstance(warehouse, WarehouseSourceReader)
    public = [
        name
        for name, _ in inspect.getmembers(WarehouseReader, inspect.isfunction)
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


def test_null_store_routing_value_is_malformed_source_data() -> None:
    executor = FakeExecutor(
        directory_rows=({"ORG_CD": "084", "ORG_IP": None, "ORG_DB": "STORE_084"},)
    )

    with pytest.raises(WarehouseReadError) as raised:
        reader(executor).discover_stores(SourceWindow(START, END))

    assert raised.value.signal == FailureSignal(
        DependencyKind.SOURCE_DATA, FailureKind.MALFORMED
    )


def test_store_routing_values_are_trimmed_before_return() -> None:
    executor = FakeExecutor(
        directory_rows=(
            {"ORG_CD": " 084 ", "ORG_IP": " 10.0.0.84 ", "ORG_DB": " STORE_084 "},
        )
    )

    store = reader(executor).discover_stores(SourceWindow(START, END)).stores[0]

    assert (store.store_code, store.org_ip, store.org_db) == (
        "084",
        "10.0.0.84",
        "STORE_084",
    )


def test_sql_server_url_requests_read_only_application_intent_without_exposing_password() -> None:
    url = URL.create(
        "mssql+pyodbc",
        username="reader",
        password="top-secret",
        host="warehouse.internal",
        database="DBWH_8555",
        query={"driver": "ODBC Driver 18 for SQL Server"},
    )

    read_only = build_read_only_url(url)

    assert read_only.query["ApplicationIntent"] == "ReadOnly"
    assert "top-secret" not in repr(read_only)


def test_engine_reads_committed_by_default_and_snapshot_only_by_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AD-020: all three source databases run with snapshot isolation OFF."""

    captured: list[dict[str, object]] = []

    def capture_create_engine(url: URL, **kwargs: object) -> Engine:
        captured.append({"url": url, **kwargs})
        return cast(Engine, object())

    monkeypatch.setattr(sql_server_module, "create_engine", capture_create_engine)
    url = URL.create("mssql+pyodbc", host="warehouse.internal", database="DBWH_8555")

    create_warehouse_engine(url)
    create_warehouse_engine(url, isolation_level="SNAPSHOT")

    assert [c["isolation_level"] for c in captured] == ["READ COMMITTED", "SNAPSHOT"]


def test_factory_uses_issue_78_settings_and_the_fixed_source_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[URL] = []

    def capture_engine(url: URL, **_: object) -> Engine:
        captured.append(url)
        return cast(Engine, object())

    monkeypatch.setattr(warehouse_module, "create_warehouse_engine", capture_engine)
    settings = Settings.model_validate(
        {
            "environment": "development",
            "database_url": "postgresql+psycopg://state@localhost/esl",
            "internal_host": "127.0.0.1",
            "source_sql_host": "warehouse.internal",
            "source_sql_username": "esl_reader",
            "source_sql_driver": "ODBC Driver 18 for SQL Server",
            "source_warehouse_database": "DBWH_8555",
        }
    )
    secrets = FakeSecrets()

    WarehouseReader.from_settings(settings, secrets)

    assert secrets.requested == [SOURCE_SQL_PASSWORD_KEY]
    assert len(captured) == 1
    assert captured[0].host == "warehouse.internal"
    assert captured[0].database == "DBWH_8555"
    assert captured[0].username == "esl_reader"
    assert captured[0].query["driver"] == "ODBC Driver 18 for SQL Server"


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (
            DriverError("08001", "tcp://warehouse:1433?password=secret"),
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
    with pytest.raises(WarehouseReadError) as raised:
        reader(FakeExecutor(error=driver_error)).discover_stores(
            SourceWindow(START, END)
        )

    assert raised.value.signal == expected
    assert "secret" not in str(raised.value).casefold()
    assert "://" not in str(raised.value)
