"""Optional read-only check against the real tb_ESL (#94).

Runs only when ``ESL_TEST_LEGACY_BASELINE_URL`` names the legacy ``ESL``
database (read-only account) and ``ESL_TEST_BASELINE_STORE_CODE`` a store.
SELECT only; the URL is never rendered; assertions are structural.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from esl_service.adapters.legacy_baseline import (
    BASELINE_QUERY_VERSION,
    SqlAlchemyLegacyBaselineExecutor,
    TbEslBaselineReader,
)
from esl_service.adapters.sql_server import create_read_only_engine
from esl_service.application.contracts import BaselineReadRequest, SourceWindow


def _test_url() -> str:
    raw = os.environ.get("ESL_TEST_LEGACY_BASELINE_URL")
    if not raw or not os.environ.get("ESL_TEST_BASELINE_STORE_CODE"):
        pytest.skip("ESL_TEST_LEGACY_BASELINE_URL and ESL_TEST_BASELINE_STORE_CODE are required")
    if make_url(raw).drivername != "mssql+pyodbc":
        raise RuntimeError("ESL_TEST_LEGACY_BASELINE_URL must use mssql+pyodbc")
    return raw


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    built = create_read_only_engine(make_url(_test_url()))
    try:
        yield built
    finally:
        built.dispose()


def test_reads_the_stores_baseline_rows_raw(engine: Engine) -> None:
    url = make_url(_test_url())
    reader = TbEslBaselineReader(
        SqlAlchemyLegacyBaselineExecutor(engine),
        instance=url.host or "test-source",
        database=url.database or "ESL",
    )
    store_code = os.environ["ESL_TEST_BASELINE_STORE_CODE"]
    now = datetime.now(UTC)

    result = reader.read_baseline(BaselineReadRequest(store_code, SourceWindow(now, now)))

    assert result.rows, "the legacy table holds rows for a processed store"
    assert {row["STORE_CODE"] for row in result.rows} == {store_code}
    for column in ("ITEM_CODE", "SALES_PRICE", "PROMOTION_TYPE", "REDLIST", "LAST_UPDATED_DATE"):
        assert column in result.rows[0]
    assert result.provenance.objects == ("dbo.tb_ESL",)
    assert result.provenance.query_version == BASELINE_QUERY_VERSION
