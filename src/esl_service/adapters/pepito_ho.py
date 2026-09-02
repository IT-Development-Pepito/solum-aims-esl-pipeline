"""Read-only PEPITO_HO adapter for ``ITEM_UOM_MAPPING_MST`` (#93, FR-001/002/025).

The procedure reaches this central iRetail table through a linked server
and applies three predicates in SQL: ``IUM_UOM_MAP_STATUS = 'O'``,
``IUM_MAIN_ITM_BARCODE = 1``, ``IUM_SALES_UOM_FLAG = 1``. Those are the
business rule that chooses the selling UOM and barcode (#36), so they are
deliberately absent here: the adapter returns every mapping row for the
requested item set, raw, so a row the domain rejects is recorded with its
reason rather than silently dropped. The only data predicate is the item
set, because the table has no store column and an unbounded read of a
central table is never what a caller means.

Everything else follows #91 through the shared ``sql_server`` helpers: one
transaction per read, a database-side UTC watermark, the configured isolation
level recorded in provenance (AD-020: READ COMMITTED by default, because the
source databases run with snapshot isolation OFF), and driver failures mapped
onto the #20 signals without their text.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from esl_service.adapters.sql_server import (
    DEFAULT_ISOLATION_LEVEL,
    READ_TIME_SQL,
    create_read_only_engine,
    failure_signal,
    watermark,
)
from esl_service.application.contracts import (
    SourceWindow,
    UomMappingReadRequest,
    UomMappingReadResult,
    WarehouseProvenance,
)
from esl_service.config import Settings
from esl_service.domain.failures import FailureSignal
from esl_service.runtime.connectivity import targets_from_settings
from esl_service.runtime.secrets import SecretProvider

__all__ = [
    "UOM_MAPPING_QUERY_VERSION",
    "PepitoHoReadError",
    "PepitoHoReader",
]

UOM_MAPPING_QUERY_VERSION = "pepito-ho-uom-current-state-v1"

#: SQL Server accepts far more parameters than this; the bound keeps one
#: statement readable in a trace and one chunk's plan cache stable.
UOM_MAPPING_CHUNK_SIZE = 500

_TARGET_NAME = "pepito-ho"
_OBJECT = "dbo.ITEM_UOM_MAPPING_MST"

_SCHEMA = (
    "SELECT IUM_ITM_CD, IUM_LEAST_UOM_CD, IUM_BAR_ITM_CD, IUM_UOM_MAP_STATUS, "
    "IUM_MAIN_ITM_BARCODE, IUM_SALES_UOM_FLAG FROM dbo.ITEM_UOM_MAPPING_MST WHERE 1 = 0"
)


@dataclass(frozen=True)
class UomMappingRows:
    """Closed transport result for one mapping read."""

    rows: tuple[Mapping[str, object], ...]
    source_watermark: datetime


class PepitoHoReadExecutor(Protocol):
    """One closed read operation; callers cannot supply SQL through this API."""

    def read_mappings(self, item_codes: Sequence[str]) -> UomMappingRows: ...


class SqlAlchemyPepitoHoExecutor:
    """SQLAlchemy transport exposing only one closed, SELECT-only operation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_mappings(self, item_codes: Sequence[str]) -> UomMappingRows:
        with self._engine.connect() as connection, connection.begin():
            _fetch_all(connection, _SCHEMA)
            source_watermark = watermark(connection.execute(text(READ_TIME_SQL)).scalar_one())
            rows: list[Mapping[str, object]] = []
            for offset in range(0, len(item_codes), UOM_MAPPING_CHUNK_SIZE):
                chunk = tuple(item_codes[offset : offset + UOM_MAPPING_CHUNK_SIZE])
                statement, parameters = _mappings_statement(chunk)
                rows.extend(_fetch_all(connection, statement, parameters))
        return UomMappingRows(tuple(rows), source_watermark)


def _mappings_statement(chunk: tuple[str, ...]) -> tuple[str, dict[str, object]]:
    names = [f"item_{index}" for index in range(len(chunk))]
    placeholders = ", ".join(f":{name}" for name in names)
    statement = f"SELECT * FROM {_OBJECT} WHERE IUM_ITM_CD IN ({placeholders})"
    return statement, dict(zip(names, chunk, strict=True))


def _fetch_all(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
) -> tuple[Mapping[str, object], ...]:
    result = connection.execute(text(statement), parameters or {})
    return tuple(dict(row) for row in result.mappings())


class PepitoHoReadError(RuntimeError):
    """A safe adapter failure carrying an existing #20 failure signal."""

    def __init__(self, signal: FailureSignal) -> None:
        super().__init__(
            f"pepito-ho read failed: {signal.dependency.value.lower()} "
            f"{signal.kind.value.lower()}"
        )
        self.signal = signal


class PepitoHoReader:
    """Implements the central-tier ``UomMappingSourceReader`` application port."""

    def __init__(
        self,
        executor: PepitoHoReadExecutor,
        *,
        instance: str,
        database: str,
        isolation_level: str = DEFAULT_ISOLATION_LEVEL,
    ) -> None:
        if not instance.strip() or not database.strip():
            raise ValueError("pepito-ho instance and database must not be blank")
        self._executor = executor
        self._instance = instance
        self._database = database
        self._isolation_level = isolation_level

    @classmethod
    def from_settings(cls, settings: Settings, secrets: SecretProvider) -> "PepitoHoReader":
        """Build from #78 configuration while keeping the password in the bundle."""

        target = next(
            target for target in targets_from_settings(settings) if target.name == _TARGET_NAME
        )
        if not target.configured():
            raise ValueError(f"{_TARGET_NAME} target is not configured")
        engine = create_read_only_engine(
            target.sqlalchemy_url(secrets.get(target.password_key)),
            isolation_level=settings.source_sql_isolation_level,
        )
        return cls(
            SqlAlchemyPepitoHoExecutor(engine),
            instance=target.host,
            database=target.database,
            isolation_level=settings.source_sql_isolation_level,
        )

    def read_mappings(self, request: UomMappingReadRequest) -> UomMappingReadResult:
        try:
            read = self._executor.read_mappings(request.item_codes)
            source_watermark = watermark(read.source_watermark)
        except PepitoHoReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise PepitoHoReadError(failure_signal(error)) from None

        return UomMappingReadResult(
            mappings=tuple(read.rows),
            provenance=self._provenance(request.source_window, source_watermark),
        )

    def _provenance(
        self, source_window: SourceWindow, source_watermark: datetime
    ) -> WarehouseProvenance:
        return WarehouseProvenance(
            instance=self._instance,
            database=self._database,
            objects=(_OBJECT,),
            query_version=UOM_MAPPING_QUERY_VERSION,
            source_window_start=source_window.start,
            source_window_end=source_window.end,
            source_watermark=source_watermark,
            isolation_level=self._isolation_level,
        )
