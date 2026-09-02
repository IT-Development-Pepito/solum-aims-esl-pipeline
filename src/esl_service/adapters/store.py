"""Read-only per-store iRetail adapter (#92, FR-001/002/025/026).

Each store runs its own iRetail SQL Server, addressed at run time from its
``DimStore`` row (``ORG_IP``, ``ORG_DB``; #91). The procedure reaches it
through a linked server it names by string concatenation; the replacement
connects directly with the same read-only account, after the address has
passed the #78 ``store_target`` validation, because this is the one place
in the system where a connection target comes from table data.

Twelve objects are read as a whole in one transaction. The procedure applies
its business predicates in SQL: ``ITM_STATUS = 'O'``, ``CMP_STATUS = 'A'``,
``CIGD_STATUS = 'O'``, validity dates, ``CMP_TYPE IN (0,1,3)``, the PFS
exclusion, ``LOC_CD = '001'``, ``STOCK_UPDATED_FLAG IS NULL``,
``BSP_PRICE_CATG = '001'``, ``BSP_STATUS = 'A'``. Every one is a domain rule
(#36, #37, #12, BR-006), so the only predicate here is the store code: the
campaign tables are joined to ``CMP_ORG_DTL`` solely to bound them to the
store, and the item and condition masters are read whole because the
database itself is the store's scope. A row the domain rejects is then
recorded with its reason instead of vanishing inside SQL.

Transport rules (AD-020) come from the shared ``sql_server`` helpers; this
tier adds a per-statement timeout, because a store link that hangs must not
hold the fan-out.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, text

from esl_service.adapters.sql_server import (
    DEFAULT_ISOLATION_LEVEL,
    READ_TIME_SQL,
    create_read_only_engine,
    failure_signal,
    watermark,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadRequest,
    StoreReadResult,
    WarehouseProvenance,
)
from esl_service.config import Settings
from esl_service.domain.failures import FailureSignal
from esl_service.runtime.connectivity import store_target
from esl_service.runtime.secrets import SecretProvider

__all__ = ["STORE_QUERY_VERSION", "StoreReadError", "StoreReader"]

STORE_QUERY_VERSION = "store-current-state-v1"

#: Provisional default (NFR-004); overridden by ``source_store_read_timeout_seconds``.
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 120

#: The columns each object must expose before any data is read; a missing
#: column is schema drift and is reported as such rather than as "no rows".
STORE_SCHEMA_PROBES: Mapping[str, str] = MappingProxyType(
    {
        "dbo.ITEM_MST": "SELECT ITM_CD FROM dbo.ITEM_MST WHERE 1 = 0",
        "dbo.ITEM_DESCRIPTION": "SELECT ITM_CD FROM dbo.ITEM_DESCRIPTION WHERE 1 = 0",
        "dbo.CMP_HDR": "SELECT CMP_GRP_CD FROM dbo.CMP_HDR WHERE 1 = 0",
        "dbo.CMP_ORG_DTL": "SELECT CMP_GRP_CD, CMP_ORG_CD FROM dbo.CMP_ORG_DTL WHERE 1 = 0",
        "dbo.CMP_ITEM_GRP_HDR": (
            "SELECT CMP_GRP_CD, CIH_GRP_CD, CIGC_CD FROM dbo.CMP_ITEM_GRP_HDR WHERE 1 = 0"
        ),
        "dbo.CMP_ITEM_GRP_CND": "SELECT CIH_GRP_CD, CND_CD FROM dbo.CMP_ITEM_GRP_CND WHERE 1 = 0",
        "dbo.CMP_CND_MST": "SELECT CND_CD FROM dbo.CMP_CND_MST WHERE 1 = 0",
        "dbo.CMP_ITEM_GRP_DTL": "SELECT CIGC_CD FROM dbo.CMP_ITEM_GRP_DTL WHERE 1 = 0",
        "dbo.STOCK_MASTER": "SELECT SM_ORG_CD, SM_ITM_CD FROM dbo.STOCK_MASTER WHERE 1 = 0",
        "dbo.OFFLINE_TEMP_ITEM_MOVEMENT": (
            "SELECT STR_ORG_CD, STR_ITM_CD FROM dbo.OFFLINE_TEMP_ITEM_MOVEMENT WHERE 1 = 0"
        ),
        "dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT": (
            "SELECT STR_ORG_CD, STR_ITM_CD FROM dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT WHERE 1 = 0"
        ),
        "dbo.BASIC_SP_MST": "SELECT BSP_ORG_CD, BSP_ITEM_CD FROM dbo.BASIC_SP_MST WHERE 1 = 0",
    }
)

_BY_STORE_CAMPAIGN = (
    " INNER JOIN dbo.CMP_ORG_DTL COD ON COD.CMP_GRP_CD = {alias}.CMP_GRP_CD"
    " WHERE COD.CMP_ORG_CD = :store_code"
)

#: The twelve reads, bounded only by store scope (see the module docstring).
STORE_SELECTS: Mapping[str, str] = MappingProxyType(
    {
        "dbo.ITEM_MST": "SELECT * FROM dbo.ITEM_MST",
        "dbo.ITEM_DESCRIPTION": "SELECT * FROM dbo.ITEM_DESCRIPTION",
        "dbo.CMP_HDR": "SELECT CH.* FROM dbo.CMP_HDR CH" + _BY_STORE_CAMPAIGN.format(alias="CH"),
        "dbo.CMP_ORG_DTL": "SELECT * FROM dbo.CMP_ORG_DTL WHERE CMP_ORG_CD = :store_code",
        "dbo.CMP_ITEM_GRP_HDR": (
            "SELECT CIGH.* FROM dbo.CMP_ITEM_GRP_HDR CIGH" + _BY_STORE_CAMPAIGN.format(alias="CIGH")
        ),
        "dbo.CMP_ITEM_GRP_CND": (
            "SELECT CIGC.* FROM dbo.CMP_ITEM_GRP_CND CIGC"
            " INNER JOIN dbo.CMP_ITEM_GRP_HDR CIGH ON CIGC.CIH_GRP_CD = CIGH.CIH_GRP_CD"
            + _BY_STORE_CAMPAIGN.format(alias="CIGH")
        ),
        "dbo.CMP_CND_MST": "SELECT * FROM dbo.CMP_CND_MST",
        "dbo.CMP_ITEM_GRP_DTL": (
            "SELECT CIGD.* FROM dbo.CMP_ITEM_GRP_DTL CIGD"
            " INNER JOIN dbo.CMP_ITEM_GRP_HDR CIGH ON CIGD.CIGC_CD = CIGH.CIGC_CD"
            + _BY_STORE_CAMPAIGN.format(alias="CIGH")
        ),
        "dbo.STOCK_MASTER": "SELECT * FROM dbo.STOCK_MASTER WHERE SM_ORG_CD = :store_code",
        "dbo.OFFLINE_TEMP_ITEM_MOVEMENT": (
            "SELECT * FROM dbo.OFFLINE_TEMP_ITEM_MOVEMENT WHERE STR_ORG_CD = :store_code"
        ),
        "dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT": (
            "SELECT * FROM dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT WHERE STR_ORG_CD = :store_code"
        ),
        "dbo.BASIC_SP_MST": "SELECT * FROM dbo.BASIC_SP_MST WHERE BSP_ORG_CD = :store_code",
    }
)


@dataclass(frozen=True)
class StoreRows:
    """Closed transport result: rows per object and the database read time."""

    rows: Mapping[str, tuple[Mapping[str, object], ...]]
    source_watermark: datetime


class StoreReadExecutor(Protocol):
    """One closed read operation; callers cannot supply SQL through this API."""

    def read_store(self, store_code: str) -> StoreRows: ...


class SqlAlchemyStoreExecutor:
    """SQLAlchemy transport exposing only one closed, SELECT-only operation."""

    def __init__(
        self, engine: Engine, *, statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS
    ) -> None:
        if statement_timeout_seconds < 1:
            raise ValueError("statement_timeout_seconds must be positive")
        self._engine = engine
        self._timeout = statement_timeout_seconds

    def read_store(self, store_code: str) -> StoreRows:
        with self._engine.connect() as connection, connection.begin():
            _apply_statement_timeout(connection, self._timeout)
            for probe in STORE_SCHEMA_PROBES.values():
                _fetch_all(connection, probe)
            source_watermark = watermark(connection.execute(text(READ_TIME_SQL)).scalar_one())
            rows: dict[str, tuple[Mapping[str, object], ...]] = {}
            for name, statement in STORE_SELECTS.items():
                parameters = {"store_code": store_code} if ":store_code" in statement else {}
                rows[name] = _fetch_all(connection, statement, parameters)
        return StoreRows(rows, source_watermark)


def _apply_statement_timeout(connection: Connection, seconds: int) -> None:
    """Bound every statement on this connection (pyodbc ``timeout``)."""

    raw = connection.connection
    dbapi: Any = getattr(raw, "dbapi_connection", raw)
    dbapi.timeout = seconds


def _fetch_all(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
) -> tuple[Mapping[str, object], ...]:
    result = connection.execute(text(statement), parameters or {})
    return tuple(dict(row) for row in result.mappings())


class StoreReadError(RuntimeError):
    """A safe adapter failure for one store, carrying an existing #20 signal."""

    def __init__(self, store_code: str, signal: FailureSignal) -> None:
        super().__init__(
            f"store {store_code} read failed: {signal.dependency.value.lower()} "
            f"{signal.kind.value.lower()}"
        )
        self.store_code = store_code
        self.signal = signal


class StoreReader:
    """Implements the per-store ``StoreSourceReader`` application port."""

    def __init__(
        self,
        executor: StoreReadExecutor,
        *,
        store: StoreDirectoryEntry,
        isolation_level: str = DEFAULT_ISOLATION_LEVEL,
        statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    ) -> None:
        self._executor = executor
        self._store = store
        self._isolation_level = isolation_level
        self._timeout = statement_timeout_seconds

    @property
    def store(self) -> StoreDirectoryEntry:
        return self._store

    @property
    def statement_timeout_seconds(self) -> int:
        return self._timeout

    @classmethod
    def from_directory_entry(
        cls, settings: Settings, secrets: SecretProvider, entry: StoreDirectoryEntry
    ) -> "StoreReader":
        """Address one store from its ``DimStore`` row; the address is validated first.

        Raises ``InvalidStoreAddress`` before any engine exists, so a row that
        is not a bare host and plain database name is never connected to.
        """

        target = store_target(
            settings, store_code=entry.store_code, org_ip=entry.org_ip, org_db=entry.org_db
        )
        engine = create_read_only_engine(
            target.sqlalchemy_url(secrets.get(target.password_key)),
            isolation_level=settings.source_sql_isolation_level,
        )
        timeout = settings.source_store_read_timeout_seconds
        return cls(
            SqlAlchemyStoreExecutor(engine, statement_timeout_seconds=timeout),
            store=entry,
            isolation_level=settings.source_sql_isolation_level,
            statement_timeout_seconds=timeout,
        )

    def read_store(self, request: StoreReadRequest) -> StoreReadResult:
        if request.store.store_code != self._store.store_code:
            raise ValueError(
                f"this reader addresses store {self._store.store_code}, "
                f"not {request.store.store_code}"
            )
        try:
            read = self._executor.read_store(self._store.store_code)
            source_watermark = watermark(read.source_watermark)
            rows = {name: tuple(read.rows[name]) for name in STORE_OBJECTS}
        except StoreReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise StoreReadError(self._store.store_code, failure_signal(error)) from None

        return StoreReadResult.from_mapping(
            rows, self._provenance(request.source_window, source_watermark)
        )

    def _provenance(
        self, source_window: SourceWindow, source_watermark: datetime
    ) -> WarehouseProvenance:
        return WarehouseProvenance(
            instance=self._store.org_ip,
            database=self._store.org_db,
            objects=STORE_OBJECTS,
            query_version=STORE_QUERY_VERSION,
            source_window_start=source_window.start,
            source_window_end=source_window.end,
            source_watermark=source_watermark,
            isolation_level=self._isolation_level,
        )
