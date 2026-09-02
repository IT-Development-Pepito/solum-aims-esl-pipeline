"""Read-only DBWH_8555 warehouse adapter (#91, FR-001/002/025/026).

The adapter owns SQL transport and source-schema names. It deliberately uses
only the store key as a data predicate: status, type, validity, PFS, UOM, and
promotion decisions are business rules and remain in the domain layer. The
supplied source evidence does not prove ``LAST_MODIFIED`` or ``LASTUPDATED``
to be complete incremental watermarks, so each call takes a transactional
current-state snapshot and records the caller's window plus database UTC time.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import URL

from esl_service.adapters.sql_server import (
    DEFAULT_ISOLATION_LEVEL,
    create_read_only_engine,
)
from esl_service.adapters.sql_server import failure_signal as _failure_signal
from esl_service.adapters.sql_server import source_text as _source_text
from esl_service.adapters.sql_server import watermark as _watermark
from esl_service.application.contracts import (
    SourceWindow,
    StoreDirectoryEntry,
    StoreDiscoveryResult,
    WarehouseProvenance,
    WarehouseReadRequest,
    WarehouseReadResult,
)
from esl_service.config import Settings
from esl_service.domain.failures import FailureSignal
from esl_service.runtime.connectivity import targets_from_settings
from esl_service.runtime.secrets import SecretProvider

__all__ = [
    "WAREHOUSE_QUERY_VERSION",
    "WarehouseReadError",
    "WarehouseReader",
]

WAREHOUSE_QUERY_VERSION = "warehouse-current-state-v1"

_DIM_STORE = "dbo.DimStore"
_DIM_ITEM_MAPPING = "dbo.DimItemMapping"
_FACT_CAMPAIGN = "dbo.FactCampaign"

_READ_TIME = "SELECT SYSUTCDATETIME() AS source_watermark"
_STORE_SCHEMA = "SELECT ORG_CD, ORG_IP, ORG_DB FROM dbo.DimStore WHERE 1 = 0"
_MAPPING_SCHEMA = "SELECT OID_ORG_CD FROM dbo.DimItemMapping WHERE 1 = 0"
_CAMPAIGN_SCHEMA = "SELECT FOR_ORGANIZATION FROM dbo.FactCampaign WHERE 1 = 0"
_STORES = "SELECT ORG_CD, ORG_IP, ORG_DB FROM dbo.DimStore ORDER BY ORG_CD"
_MAPPINGS = "SELECT * FROM dbo.DimItemMapping WHERE OID_ORG_CD = :store_code"
_CAMPAIGNS = "SELECT * FROM dbo.FactCampaign WHERE FOR_ORGANIZATION = :store_code"



@dataclass(frozen=True)
class WarehouseDirectoryRows:
    """Closed transport result for one directory read."""

    rows: tuple[Mapping[str, object], ...]
    source_watermark: datetime


@dataclass(frozen=True)
class WarehouseStoreRows:
    """Closed transport result for one store data read."""

    item_mappings: tuple[Mapping[str, object], ...]
    campaigns: tuple[Mapping[str, object], ...]
    source_watermark: datetime


class WarehouseReadExecutor(Protocol):
    """Closed read operations; callers cannot supply SQL through this API."""

    def discover_stores(self) -> WarehouseDirectoryRows: ...

    def read_store(self, store_code: str) -> WarehouseStoreRows: ...


class SqlAlchemyWarehouseExecutor:
    """SQLAlchemy transport exposing only two closed, SELECT-only operations."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def discover_stores(self) -> WarehouseDirectoryRows:
        with self._engine.connect() as connection, connection.begin():
            _fetch_all(connection, _STORE_SCHEMA)
            source_watermark = _watermark(
                connection.execute(text(_READ_TIME)).scalar_one()
            )
            rows = _fetch_all(connection, _STORES)
        return WarehouseDirectoryRows(rows, source_watermark)

    def read_store(self, store_code: str) -> WarehouseStoreRows:
        with self._engine.connect() as connection, connection.begin():
            _fetch_all(connection, _MAPPING_SCHEMA)
            _fetch_all(connection, _CAMPAIGN_SCHEMA)
            source_watermark = _watermark(
                connection.execute(text(_READ_TIME)).scalar_one()
            )
            parameters: Mapping[str, object] = {"store_code": store_code}
            mappings = _fetch_all(connection, _MAPPINGS, parameters)
            campaigns = _fetch_all(connection, _CAMPAIGNS, parameters)
        return WarehouseStoreRows(mappings, campaigns, source_watermark)


def _fetch_all(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
) -> tuple[Mapping[str, object], ...]:
    result = connection.execute(text(statement), parameters or {})
    return tuple(dict(row) for row in result.mappings())


class WarehouseReadError(RuntimeError):
    """A safe adapter failure carrying an existing #20 failure signal."""

    def __init__(self, signal: FailureSignal) -> None:
        super().__init__(
            f"warehouse read failed: {signal.dependency.value.lower()} "
            f"{signal.kind.value.lower()}"
        )
        self.signal = signal


def build_read_only_url(url: URL) -> URL:
    """Request SQL Server read intent without changing or rendering credentials."""

    return url.update_query_dict({"ApplicationIntent": "ReadOnly"})


def create_warehouse_engine(
    url: URL, *, isolation_level: str = DEFAULT_ISOLATION_LEVEL
) -> Engine:
    """Create the shared-tier SQL Server engine (AD-020 isolation, bounded connect)."""

    return create_read_only_engine(url, isolation_level=isolation_level)


class WarehouseReader:
    """Implements the shared-tier ``WarehouseSourceReader`` application port."""

    def __init__(
        self,
        executor: WarehouseReadExecutor,
        *,
        instance: str,
        database: str,
        isolation_level: str = DEFAULT_ISOLATION_LEVEL,
    ) -> None:
        if not instance.strip() or not database.strip():
            raise ValueError("warehouse instance and database must not be blank")
        self._executor = executor
        self._instance = instance
        self._database = database
        self._isolation_level = isolation_level

    @classmethod
    def from_settings(
        cls, settings: Settings, secrets: SecretProvider
    ) -> "WarehouseReader":
        """Build from #78 configuration while keeping the password in the bundle."""

        target = next(
            target
            for target in targets_from_settings(settings)
            if target.name == "warehouse"
        )
        if not target.configured():
            raise ValueError("warehouse target is not configured")
        engine = create_warehouse_engine(
            target.sqlalchemy_url(secrets.get(target.password_key)),
            isolation_level=settings.source_sql_isolation_level,
        )
        return cls(
            SqlAlchemyWarehouseExecutor(engine),
            instance=target.host,
            database=target.database,
            isolation_level=settings.source_sql_isolation_level,
        )

    def discover_stores(self, source_window: SourceWindow) -> StoreDiscoveryResult:
        try:
            read = self._executor.discover_stores()
            stores = tuple(
                StoreDirectoryEntry(
                    store_code=_source_text(row, "ORG_CD"),
                    org_ip=_source_text(row, "ORG_IP"),
                    org_db=_source_text(row, "ORG_DB"),
                )
                for row in read.rows
            )
        except WarehouseReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise WarehouseReadError(_failure_signal(error)) from None

        return StoreDiscoveryResult(
            stores=stores,
            provenance=self._provenance(
                objects=(_DIM_STORE,),
                source_window=source_window,
                source_watermark=read.source_watermark,
            ),
        )

    def read_store(self, request: WarehouseReadRequest) -> WarehouseReadResult:
        try:
            read = self._executor.read_store(request.store_code)
        except WarehouseReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise WarehouseReadError(_failure_signal(error)) from None

        return WarehouseReadResult(
            item_mappings=read.item_mappings,
            campaigns=read.campaigns,
            provenance=self._provenance(
                objects=(_DIM_ITEM_MAPPING, _FACT_CAMPAIGN),
                source_window=request.source_window,
                source_watermark=read.source_watermark,
            ),
        )

    def _provenance(
        self,
        *,
        objects: tuple[str, ...],
        source_window: SourceWindow,
        source_watermark: datetime,
    ) -> WarehouseProvenance:
        return WarehouseProvenance(
            instance=self._instance,
            database=self._database,
            objects=objects,
            query_version=WAREHOUSE_QUERY_VERSION,
            source_window_start=source_window.start,
            source_window_end=source_window.end,
            source_watermark=source_watermark,
            isolation_level=self._isolation_level,
        )
