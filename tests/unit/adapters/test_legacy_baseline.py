"""Read-only tb_ESL baseline adapter (#94, FR-021, FR-022).

The baseline is read raw, per store, under the configured isolation, with
the same provenance shape as the source tiers. Two things make it different
from a source adapter: it may only be built under ``ESL_SHADOW_MODE``, and
no ingestion module may import it, which an import-graph test enforces.
"""

import ast
import inspect
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from esl_service.adapters import legacy_baseline as baseline_module
from esl_service.adapters.legacy_baseline import (
    BASELINE_QUERY_VERSION,
    BaselineNotAllowed,
    BaselineRows,
    LegacyBaselineReadError,
    SqlAlchemyLegacyBaselineExecutor,
    TbEslBaselineReader,
)
from esl_service.application.contracts import (
    BaselineReadRequest,
    LegacyBaselineReader,
    SourceWindow,
)
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.secrets import SOURCE_SQL_PASSWORD_KEY

START = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
END = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
DATABASE_NOW = datetime(2026, 9, 2, 2, 0, 1, tzinfo=UTC)
SCHEMA = "SELECT STORE_CODE, ITEM_CODE, LAST_UPDATED_DATE, SYNC_REC FROM dbo.tb_ESL WHERE 1 = 0"
READ_TIME = "SELECT SYSUTCDATETIME() AS source_watermark"
ROWS = "SELECT * FROM dbo.tb_ESL WHERE STORE_CODE = :store_code"

RAW_ROWS: tuple[Mapping[str, object], ...] = (
    {"STORE_CODE": "084", "ITEM_CODE": "SKU-1", "SALES_PRICE": 12500, "REDLIST": "", "SYNC_REC": 1},
    {"STORE_CODE": "084", "ITEM_CODE": "SKU-2", "SALES_PRICE": None, "REDLIST": "", "SYNC_REC": 0},
)


class FakeExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.transactions = 0
        self.requested: list[str] = []

    def read_baseline(self, store_code: str) -> BaselineRows:
        self.transactions += 1
        self.requested.append(store_code)
        if self.error is not None:
            raise self.error
        return BaselineRows(RAW_ROWS, DATABASE_NOW)


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


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.begins = 0

    def begin(self) -> Any:
        self.begins += 1
        return nullcontext()

    def execute(self, statement: object, parameters: Mapping[str, object] | None = None) -> RecordingResult:
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        if sql == READ_TIME:
            return RecordingResult(scalar=DATABASE_NOW)
        if sql == ROWS:
            return RecordingResult(RAW_ROWS)
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


def reader(executor: FakeExecutor) -> TbEslBaselineReader:
    return TbEslBaselineReader(executor, instance="sql.internal", database="ESL")


def request(store_code: str = "084") -> BaselineReadRequest:
    return BaselineReadRequest(store_code, SourceWindow(START, END))


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://state@localhost/esl",
        "internal_host": "127.0.0.1",
        "source_sql_host": "sql.internal",
        "source_sql_username": "esl_reader",
        "source_sql_driver": "ODBC Driver 18 for SQL Server",
        "legacy_baseline_database": "ESL",
    }
    values.update(overrides)
    return Settings.model_validate(values)


# --- raw rows, one transaction, honest provenance -------------------------------


def test_returns_the_stores_rows_raw_including_empty_redlist_and_nulls() -> None:
    executor = FakeExecutor()

    result = reader(executor).read_baseline(request())

    assert executor.transactions == 1 and executor.requested == ["084"]
    assert [row["ITEM_CODE"] for row in result.rows] == ["SKU-1", "SKU-2"]
    assert result.rows[0]["REDLIST"] == ""  # expected empty (source-owner direction), not a mismatch
    assert result.rows[1]["SALES_PRICE"] is None


def test_provenance_names_the_legacy_instance_and_table() -> None:
    provenance = reader(FakeExecutor()).read_baseline(request()).provenance

    assert provenance.instance == "sql.internal"
    assert provenance.database == "ESL"
    assert provenance.objects == ("dbo.tb_ESL",)
    assert provenance.query_version == BASELINE_QUERY_VERSION
    assert provenance.source_window_start == START
    assert provenance.source_window_end == END
    assert provenance.source_watermark == DATABASE_NOW
    assert provenance.isolation_level == "READ COMMITTED"


# --- the concrete executor ------------------------------------------------------------


def test_concrete_executor_emits_only_the_exact_approved_selects_in_one_transaction() -> None:
    engine = RecordingEngine()

    rows = SqlAlchemyLegacyBaselineExecutor(cast(Engine, engine)).read_baseline("084")

    assert engine.connection.begins == 1
    assert engine.connection.calls == [(SCHEMA, {}), (READ_TIME, {}), (ROWS, {"store_code": "084"})]
    assert all(sql.startswith("SELECT ") for sql, _ in engine.connection.calls)
    assert len(rows.rows) == 2


def test_production_facing_api_accepts_no_caller_supplied_sql() -> None:
    assert baseline_module.__all__ == [
        "BASELINE_QUERY_VERSION",
        "BaselineNotAllowed",
        "LegacyBaselineReadError",
        "TbEslBaselineReader",
    ]
    methods = {
        n for n, _ in inspect.getmembers(SqlAlchemyLegacyBaselineExecutor, inspect.isfunction)
        if not n.startswith("_")
    }
    assert methods == {"read_baseline"}


def test_reader_satisfies_the_port_and_exposes_no_write_api() -> None:
    baseline = reader(FakeExecutor())

    assert isinstance(baseline, LegacyBaselineReader)
    public = [n for n, _ in inspect.getmembers(TbEslBaselineReader, inspect.isfunction) if not n.startswith("_")]
    assert public
    assert not [
        n
        for n in public
        if n.casefold().startswith(("insert", "update", "delete", "write", "set_", "create", "drop", "truncate"))
    ]


# --- shadow-mode only (acceptance criterion) -------------------------------------------


def test_factory_builds_only_under_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[URL, str]] = []

    def capture_engine(url: URL, *, isolation_level: str) -> Engine:
        captured.append((url, isolation_level))
        return cast(Engine, object())

    monkeypatch.setattr(baseline_module, "create_read_only_engine", capture_engine)
    secrets = FakeSecrets()

    built = TbEslBaselineReader.from_settings(settings(shadow_mode=True), secrets)

    assert secrets.requested == [SOURCE_SQL_PASSWORD_KEY]
    ((url, level),) = captured
    assert url.host == "sql.internal" and url.database == "ESL" and url.username == "esl_reader"
    assert level == "READ COMMITTED"
    assert "test-only-password" not in repr(built)


def test_factory_refuses_outside_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(url: URL, *, isolation_level: str) -> Engine:
        raise AssertionError("no engine may be created outside shadow mode")

    monkeypatch.setattr(baseline_module, "create_read_only_engine", never)

    with pytest.raises(BaselineNotAllowed, match="shadow"):
        TbEslBaselineReader.from_settings(settings(shadow_mode=False), FakeSecrets())


def test_factory_refuses_an_unconfigured_tier() -> None:
    with pytest.raises(ValueError, match="legacy-baseline"):
        TbEslBaselineReader.from_settings(settings(source_sql_host=""), FakeSecrets())


def test_no_ingestion_or_domain_module_imports_the_baseline_reader() -> None:
    """The baseline is unreachable from any ingestion or domain code path.

    The one permitted importer is the runtime composition root (#102,
    ``runtime/host.py``), which hands the baseline to the shadow comparison
    and returns nothing outside shadow mode; ``domain``, ``application``,
    ``adapters``, ``persistence``, and ``web`` may never import it.
    """

    source_root = Path(baseline_module.__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if path.name == "legacy_baseline.py":
            continue
        if path.relative_to(source_root).parts[0] == "runtime":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any("legacy_baseline" in name for name in names):
                offenders.append(str(path.relative_to(source_root)))
    assert offenders == []


# --- failures ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("driver_error", "expected"),
    [
        (DriverError("08001", "tcp://sql:1433?password=secret"), FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)),
        (DriverError("28000", "login failed; password=secret"), FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED)),
        (DriverError("42S02", "invalid object dbo.tb_ESL; password=secret"), FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)),
        (DriverError("HYT00", "timeout; password=secret"), FailureSignal(DependencyKind.SQL_SERVER, FailureKind.TIMEOUT)),
    ],
)
def test_failures_map_to_safe_existing_signals(driver_error: DriverError, expected: FailureSignal) -> None:
    with pytest.raises(LegacyBaselineReadError) as raised:
        reader(FakeExecutor(error=driver_error)).read_baseline(request())

    assert raised.value.signal == expected
    assert "secret" not in str(raised.value).casefold()
    assert "://" not in str(raised.value)
