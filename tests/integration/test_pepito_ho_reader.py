"""Optional read-only PEPITO_HO checks (#93, FR-001/002/025).

These tests run only when ``ESL_TEST_PEPITO_HO_URL`` explicitly names a
non-production (or read-only) SQL Server holding ``ITEM_UOM_MAPPING_MST``.
They issue SELECT statements only, never render the URL, and make structural
assertions because source rows are expected to change.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from esl_service.adapters.pepito_ho import (
    UOM_MAPPING_QUERY_VERSION,
    PepitoHoReader,
    SqlAlchemyPepitoHoExecutor,
    create_read_only_engine,
)
from esl_service.application.contracts import SourceWindow, UomMappingReadRequest


def _test_url() -> str:
    raw = os.environ.get("ESL_TEST_PEPITO_HO_URL")
    if not raw:
        pytest.skip("ESL_TEST_PEPITO_HO_URL is required for PEPITO_HO integration")
    if make_url(raw).drivername != "mssql+pyodbc":
        raise RuntimeError("ESL_TEST_PEPITO_HO_URL must use mssql+pyodbc")
    return raw


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    built = create_read_only_engine(make_url(_test_url()))
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture(scope="module")
def uom_reader(engine: Engine) -> PepitoHoReader:
    url = make_url(_test_url())
    return PepitoHoReader(
        SqlAlchemyPepitoHoExecutor(engine),
        instance=url.host or "configured-test-source",
        database=url.database or "PEPITO_HO",
    )


def test_reads_raw_mapping_rows_for_a_small_live_item_set(
    engine: Engine, uom_reader: PepitoHoReader
) -> None:
    with engine.connect() as connection:
        sample = [
            row[0]
            for row in connection.execute(
                text("SELECT TOP 3 IUM_ITM_CD FROM dbo.ITEM_UOM_MAPPING_MST ORDER BY IUM_ITM_CD")
            )
        ]
    if not sample:
        pytest.skip("ITEM_UOM_MAPPING_MST holds no rows on the test source")

    now = datetime.now(UTC)
    result = uom_reader.read_mappings(UomMappingReadRequest(tuple(sample), SourceWindow(now, now)))

    assert result.mappings
    assert {row["IUM_ITM_CD"] for row in result.mappings} <= set(sample)
    for column in ("IUM_LEAST_UOM_CD", "IUM_BAR_ITM_CD", "IUM_UOM_MAP_STATUS"):
        assert column in result.mappings[0]
    assert result.provenance.objects == ("dbo.ITEM_UOM_MAPPING_MST",)
    assert result.provenance.query_version == UOM_MAPPING_QUERY_VERSION
    assert result.provenance.source_watermark.tzinfo is not None


def test_a_read_completes_under_the_default_isolation(engine: Engine) -> None:
    """AD-020: the source databases run with snapshot isolation OFF, so the
    default engine must be able to read inside a transaction, and the level
    it reads under must be the one provenance will record."""

    with engine.connect() as connection, connection.begin():
        level = connection.execute(
            text(
                "SELECT transaction_isolation_level FROM sys.dm_exec_sessions "
                "WHERE session_id = @@SPID"
            )
        ).scalar_one()
        connection.execute(text("SELECT TOP 1 IUM_ITM_CD FROM dbo.ITEM_UOM_MAPPING_MST")).all()

    assert level == 2  # READ COMMITTED
