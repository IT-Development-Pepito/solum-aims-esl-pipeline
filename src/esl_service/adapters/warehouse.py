"""Read-only DBWH_8555 warehouse adapter (#91, FR-001/002/025/026).

The adapter owns SQL transport and source-schema names. It deliberately uses
only the store key as a data predicate: status, type, validity, PFS, UOM, and
promotion decisions are business rules and remain in the domain layer. The
supplied source evidence does not prove ``LAST_MODIFIED`` or ``LASTUPDATED``
to be complete incremental watermarks, so each call takes a transactional
current-state snapshot and records the caller's window plus database UTC time.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL

from esl_service.application.contracts import (
    SourceWindow,
    StoreDirectoryEntry,
    StoreDiscoveryResult,
    WarehouseProvenance,
    WarehouseReadRequest,
    WarehouseReadResult,
)
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.connectivity import (
    ProbeOutcome,
    classify_failure,
    targets_from_settings,
)
from esl_service.runtime.secrets import SecretProvider

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

_SCHEMA_SQLSTATE_PREFIXES = ("42",)


class SqlReadSession(Protocol):
    """The SELECT-only capability exposed to the warehouse reader."""

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> Sequence[Mapping[str, object]]: ...

    def fetch_scalar(self, statement: str) -> object: ...


class WarehouseReadExecutor(Protocol):
    """Opens one bounded read transaction without exposing mutation methods."""

    def read_transaction(self) -> AbstractContextManager[SqlReadSession]: ...


class _SqlAlchemyReadSession:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> Sequence[Mapping[str, object]]:
        result = self._connection.execute(text(statement), parameters or {})
        return tuple(dict(row) for row in result.mappings())

    def fetch_scalar(self, statement: str) -> object:
        return self._connection.execute(text(statement)).scalar_one()


class SqlAlchemyWarehouseExecutor:
    """SQLAlchemy transport whose public capability is one read transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def read_transaction(self) -> Iterator[SqlReadSession]:
        with self._engine.connect() as connection, connection.begin():
            yield _SqlAlchemyReadSession(connection)


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


def create_warehouse_engine(url: URL) -> Engine:
    """Create the shared-tier SQL Server engine with bounded connection time."""

    return create_engine(
        build_read_only_url(url),
        connect_args={"timeout": 10},
        pool_pre_ping=True,
    )


def _walk_errors(error: BaseException) -> Iterator[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        yield current
        current = getattr(current, "orig", None) or current.__cause__


def _is_schema_drift(error: BaseException) -> bool:
    for current in _walk_errors(error):
        sqlstate = getattr(current, "sqlstate", None)
        first_arg = current.args[0] if current.args else None
        for candidate in (sqlstate, first_arg):
            if isinstance(candidate, str) and candidate.startswith(
                _SCHEMA_SQLSTATE_PREFIXES
            ):
                return True
    return False


def _failure_signal(error: BaseException) -> FailureSignal:
    if _is_schema_drift(error) or isinstance(error, (KeyError, TypeError, ValueError)):
        return FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)

    outcome = classify_failure(error)
    if outcome is ProbeOutcome.CREDENTIAL_REJECTED:
        return FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED)
    if outcome is ProbeOutcome.DRIVER_MISSING:
        return FailureSignal(DependencyKind.CONFIGURATION, FailureKind.MALFORMED)
    return FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)


def _watermark(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("warehouse source watermark is not a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class WarehouseReader:
    """Implements the shared-tier ``WarehouseSourceReader`` application port."""

    def __init__(
        self,
        executor: WarehouseReadExecutor,
        *,
        instance: str,
        database: str,
    ) -> None:
        if not instance.strip() or not database.strip():
            raise ValueError("warehouse instance and database must not be blank")
        self._executor = executor
        self._instance = instance
        self._database = database

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
            target.sqlalchemy_url(secrets.get(target.password_key))
        )
        return cls(
            SqlAlchemyWarehouseExecutor(engine),
            instance=target.host,
            database=target.database,
        )

    def discover_stores(self, source_window: SourceWindow) -> StoreDiscoveryResult:
        try:
            with self._executor.read_transaction() as session:
                session.fetch_all(_STORE_SCHEMA)
                source_watermark = _watermark(session.fetch_scalar(_READ_TIME))
                rows = session.fetch_all(_STORES)
                stores = tuple(
                    StoreDirectoryEntry(
                        store_code=str(row["ORG_CD"]),
                        org_ip=str(row["ORG_IP"]),
                        org_db=str(row["ORG_DB"]),
                    )
                    for row in rows
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
                source_watermark=source_watermark,
            ),
        )

    def read_store(self, request: WarehouseReadRequest) -> WarehouseReadResult:
        try:
            with self._executor.read_transaction() as session:
                session.fetch_all(_MAPPING_SCHEMA)
                session.fetch_all(_CAMPAIGN_SCHEMA)
                source_watermark = _watermark(session.fetch_scalar(_READ_TIME))
                parameters: Mapping[str, object] = {"store_code": request.store_code}
                mappings = tuple(
                    dict(row) for row in session.fetch_all(_MAPPINGS, parameters)
                )
                campaigns = tuple(
                    dict(row) for row in session.fetch_all(_CAMPAIGNS, parameters)
                )
        except WarehouseReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise WarehouseReadError(_failure_signal(error)) from None

        return WarehouseReadResult(
            item_mappings=mappings,
            campaigns=campaigns,
            provenance=self._provenance(
                objects=(_DIM_ITEM_MAPPING, _FACT_CAMPAIGN),
                source_window=request.source_window,
                source_watermark=source_watermark,
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
        )
