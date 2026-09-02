"""Optional read-only checks against one real store server (#92).

These tests run only when ``ESL_TEST_STORE_SQL_URL`` names one store's
iRetail SQL Server (read-only account) and ``ESL_TEST_STORE_CODE`` its store
code. They issue SELECT statements only, never render the URL, and make
structural assertions because store rows change continuously.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from esl_service.adapters.sql_server import create_read_only_engine
from esl_service.adapters.store import (
    STORE_QUERY_VERSION,
    SqlAlchemyStoreExecutor,
    StoreReader,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadRequest,
)


def _test_url() -> str:
    raw = os.environ.get("ESL_TEST_STORE_SQL_URL")
    if not raw or not os.environ.get("ESL_TEST_STORE_CODE"):
        pytest.skip("ESL_TEST_STORE_SQL_URL and ESL_TEST_STORE_CODE are required")
    if make_url(raw).drivername != "mssql+pyodbc":
        raise RuntimeError("ESL_TEST_STORE_SQL_URL must use mssql+pyodbc")
    return raw


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    built = create_read_only_engine(make_url(_test_url()))
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture(scope="module")
def store() -> StoreDirectoryEntry:
    url = make_url(_test_url())
    return StoreDirectoryEntry(
        os.environ["ESL_TEST_STORE_CODE"], url.host or "test-store", url.database or "STORE"
    )


def test_reads_all_twelve_objects_raw_for_one_live_store(
    engine: Engine, store: StoreDirectoryEntry
) -> None:
    reader = StoreReader(SqlAlchemyStoreExecutor(engine, statement_timeout_seconds=120), store=store)
    now = datetime.now(UTC)

    result = reader.read_store(StoreReadRequest(store, SourceWindow(now, now)))

    rows = result.as_mapping()
    assert set(rows) == set(STORE_OBJECTS)
    assert rows["dbo.ITEM_MST"], "a store without items is not a store"
    assert result.provenance.query_version == STORE_QUERY_VERSION
    assert result.provenance.database == store.org_db
    assert result.provenance.source_watermark.tzinfo is not None
