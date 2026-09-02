"""Optional read-only DBWH_8555 checks (#91, FR-001/002/025/026).

These tests run only when ``ESL_TEST_SOURCE_SQL_URL`` explicitly names a
non-production SQL Server source. They issue SELECT statements only, never
render the URL, and make structural assertions because source row counts are
expected to change.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from esl_service.adapters.warehouse import (
    SqlAlchemyWarehouseExecutor,
    WarehouseReader,
    create_warehouse_engine,
)
from esl_service.application.contracts import SourceWindow, WarehouseReadRequest


def _test_source_url() -> str:
    raw = os.environ.get("ESL_TEST_SOURCE_SQL_URL")
    if not raw:
        pytest.skip("ESL_TEST_SOURCE_SQL_URL is required for warehouse integration")
    url = make_url(raw)
    if url.drivername != "mssql+pyodbc":
        raise RuntimeError("ESL_TEST_SOURCE_SQL_URL must use mssql+pyodbc")
    return raw


@pytest.fixture(scope="module")
def warehouse_engine() -> Iterator[Engine]:
    engine = create_warehouse_engine(make_url(_test_source_url()))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def warehouse_reader(warehouse_engine: Engine) -> WarehouseReader:
    url = make_url(_test_source_url())
    return WarehouseReader(
        SqlAlchemyWarehouseExecutor(warehouse_engine),
        instance=url.host or "configured-test-source",
        database=url.database or "DBWH_8555",
    )


def test_discovers_store_routes_and_reads_one_store_snapshot(
    warehouse_reader: WarehouseReader,
) -> None:
    now = datetime.now(UTC)
    window = SourceWindow(now, now)
    directory = warehouse_reader.discover_stores(window)

    assert directory.provenance.objects == ("dbo.DimStore",)
    assert directory.provenance.source_window_start == now
    assert directory.provenance.source_window_end == now
    assert len({store.store_code for store in directory.stores}) == len(
        directory.stores
    )
    if not directory.stores:
        pytest.skip("the configured non-production source has no stores")

    snapshot = warehouse_reader.read_store(
        WarehouseReadRequest(directory.stores[0].store_code, window)
    )
    assert snapshot.provenance.source_window_start == now
    assert snapshot.provenance.source_window_end == now
    assert snapshot.provenance.objects == (
        "dbo.DimItemMapping",
        "dbo.FactCampaign",
    )


def test_configured_identity_has_no_write_permission(warehouse_engine: Engine) -> None:
    """The test role supplies the authorization boundary behind read intent."""

    objects = ("dbo.DimStore", "dbo.DimItemMapping", "dbo.FactCampaign")
    with warehouse_engine.connect() as connection:
        for object_name in objects:
            permissions = connection.execute(
                text(
                    "SELECT "
                    "HAS_PERMS_BY_NAME(:object_name, 'OBJECT', 'INSERT'), "
                    "HAS_PERMS_BY_NAME(:object_name, 'OBJECT', 'UPDATE'), "
                    "HAS_PERMS_BY_NAME(:object_name, 'OBJECT', 'DELETE')"
                ),
                {"object_name": object_name},
            ).one()
            assert tuple(permissions) == (0, 0, 0)


def test_configured_source_supports_transaction_level_snapshot_isolation(
    warehouse_engine: Engine,
) -> None:
    """No weaker read-committed fallback may be labelled a coherent snapshot."""

    with warehouse_engine.connect() as connection, connection.begin():
        isolation_level = connection.execute(
            text(
                "SELECT transaction_isolation_level "
                "FROM sys.dm_exec_sessions WHERE session_id = @@SPID"
            )
        ).scalar_one()

    assert isolation_level == 5, "SQL Server reports SNAPSHOT as isolation level 5"
