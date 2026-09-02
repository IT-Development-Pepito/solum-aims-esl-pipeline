"""Read-only DBWH_8555 adapter (#91, FR-001, FR-002, FR-025, FR-026)."""

import inspect
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import warehouse as warehouse_module
from esl_service.adapters.warehouse import (
    WAREHOUSE_QUERY_VERSION,
    SqlReadSession,
    WarehouseReader,
    WarehouseReadError,
    build_read_only_url,
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


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object] | None]] = []

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append((statement, parameters))
        if "DimStore" in statement and "WHERE 1 = 0" not in statement:
            return (
                {"ORG_CD": "084", "ORG_IP": "10.0.0.84", "ORG_DB": "STORE_084"},
                {"ORG_CD": "085", "ORG_IP": "10.0.0.85", "ORG_DB": "STORE_085"},
            )
        if "DimItemMapping" in statement and "WHERE 1 = 0" not in statement:
            return (
                {"OID_ORG_CD": "084", "OID_ITM_CD": "SKU-1", "OID_ITM_STATUS": "C"},
            )
        if "FactCampaign" in statement and "WHERE 1 = 0" not in statement:
            return (
                {
                    "FOR_ORGANIZATION": "084",
                    "CAMPAIGN_STATUS": "INACTIVE",
                    "PFS": "Y",
                    "CAMPAIGN_TYPE": "UNSUPPORTED-EVIDENCE",
                },
            )
        return ()

    def fetch_scalar(self, statement: str) -> object:
        self.calls.append((statement, None))
        return DATABASE_NOW


class FakeExecutor:
    def __init__(
        self, session: FakeSession | None = None, error: Exception | None = None
    ) -> None:
        self.session = session or FakeSession()
        self.error = error
        self.transactions = 0

    @contextmanager
    def read_transaction(self) -> Iterator[SqlReadSession]:
        self.transactions += 1
        if self.error is not None:
            raise self.error
        yield self.session


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
    assert result.provenance.query_version == WAREHOUSE_QUERY_VERSION
    assert result.provenance.source_watermark == DATABASE_NOW
    assert result.provenance.source_window_start == START
    assert result.provenance.source_window_end == END
    assert executor.transactions == 1


def test_reads_raw_rows_for_one_store_in_one_transaction_without_business_filters() -> (
    None
):
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

    statements = "\n".join(statement for statement, _ in executor.session.calls)
    folded = statements.casefold()
    assert all(
        statement.lstrip().casefold().startswith("select")
        for statement, _ in executor.session.calls
    )
    assert "oid_org_cd = :store_code" in folded
    assert "for_organization = :store_code" in folded
    for forbidden in (
        "oid_itm_status =",
        "campaign_status =",
        "campaign_type =",
        "pfs =",
        "last_modified >",
        "lastupdated >",
        "getdate(",
    ):
        assert forbidden not in folded
    assert all(
        parameters in (None, {"store_code": "084"})
        for _, parameters in executor.session.calls
    )


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
            (
                "insert",
                "update",
                "delete",
                "write",
                "set_",
                "create",
                "drop",
                "truncate",
            )
        )
    ]


def test_sql_server_url_requests_read_only_application_intent_without_exposing_password() -> (
    None
):
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


def test_factory_uses_issue_78_settings_and_the_fixed_source_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[URL] = []

    def capture_engine(url: URL) -> Engine:
        captured.append(url)
        return object()  # type: ignore[return-value]

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
