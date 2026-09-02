"""Read-only ``tb_ESL`` parity-baseline adapter, shadow mode only (#94).

``tb_ESL`` is what the legacy procedure writes (SYSTEM_ARCHITECTURE inventory,
corrected in PR #80). It is not an input: the replacement reads the same
three tiers the procedure reads and computes in the domain. Under
``ESL_SHADOW_MODE`` its rows are read, raw and per store, as the baseline
the computed canonical records are compared against (FR-021, FR-022, and
the #37 deployed-parity evaluation). Comparison semantics, tolerances, and
mismatch classification belong to that comparison work, not here; ``REDLIST``
is expected to be empty (source-owner direction) and is returned as it is.

Two guards keep this a baseline and not a back door. ``from_settings``
refuses to build outside shadow mode, and an import-graph test refuses any
other module that imports this one, so no ingestion or domain path can reach
``tb_ESL``. Transport rules (AD-020) come from the shared ``sql_server``
helpers.
"""

from collections.abc import Mapping
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
    BaselineReadRequest,
    BaselineReadResult,
    SourceWindow,
    WarehouseProvenance,
)
from esl_service.config import Settings
from esl_service.domain.failures import FailureSignal
from esl_service.runtime.connectivity import targets_from_settings
from esl_service.runtime.secrets import SecretProvider

__all__ = [
    "BASELINE_QUERY_VERSION",
    "BaselineNotAllowed",
    "LegacyBaselineReadError",
    "TbEslBaselineReader",
]

BASELINE_QUERY_VERSION = "tb-esl-baseline-v1"

_TARGET_NAME = "legacy-baseline"
_OBJECT = "dbo.tb_ESL"
_SCHEMA = "SELECT STORE_CODE, ITEM_CODE, LAST_UPDATED_DATE, SYNC_REC FROM dbo.tb_ESL WHERE 1 = 0"
_ROWS = "SELECT * FROM dbo.tb_ESL WHERE STORE_CODE = :store_code"


class BaselineNotAllowed(RuntimeError):
    """Raised when the baseline reader is requested outside shadow mode."""


@dataclass(frozen=True)
class BaselineRows:
    """Closed transport result for one baseline read."""

    rows: tuple[Mapping[str, object], ...]
    source_watermark: datetime


class LegacyBaselineReadExecutor(Protocol):
    """One closed read operation; callers cannot supply SQL through this API."""

    def read_baseline(self, store_code: str) -> BaselineRows: ...


class SqlAlchemyLegacyBaselineExecutor:
    """SQLAlchemy transport exposing only one closed, SELECT-only operation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_baseline(self, store_code: str) -> BaselineRows:
        with self._engine.connect() as connection, connection.begin():
            _fetch_all(connection, _SCHEMA)
            source_watermark = watermark(connection.execute(text(READ_TIME_SQL)).scalar_one())
            rows = _fetch_all(connection, _ROWS, {"store_code": store_code})
        return BaselineRows(rows, source_watermark)


def _fetch_all(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
) -> tuple[Mapping[str, object], ...]:
    result = connection.execute(text(statement), parameters or {})
    return tuple(dict(row) for row in result.mappings())


class LegacyBaselineReadError(RuntimeError):
    """A safe adapter failure carrying an existing #20 failure signal."""

    def __init__(self, signal: FailureSignal) -> None:
        super().__init__(
            f"legacy baseline read failed: {signal.dependency.value.lower()} "
            f"{signal.kind.value.lower()}"
        )
        self.signal = signal


class TbEslBaselineReader:
    """Implements the ``LegacyBaselineReader`` port over the legacy ``ESL`` database."""

    def __init__(
        self,
        executor: LegacyBaselineReadExecutor,
        *,
        instance: str,
        database: str,
        isolation_level: str = DEFAULT_ISOLATION_LEVEL,
    ) -> None:
        if not instance.strip() or not database.strip():
            raise ValueError("legacy-baseline instance and database must not be blank")
        self._executor = executor
        self._instance = instance
        self._database = database
        self._isolation_level = isolation_level

    @classmethod
    def from_settings(cls, settings: Settings, secrets: SecretProvider) -> "TbEslBaselineReader":
        """Build from #78 configuration, in shadow mode only; the password stays in the bundle."""

        if not settings.shadow_mode:
            raise BaselineNotAllowed(
                "the tb_ESL baseline may be read only in shadow mode (ESL_SHADOW_MODE=true); "
                "it is a parity baseline, not a source"
            )
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
            SqlAlchemyLegacyBaselineExecutor(engine),
            instance=target.host,
            database=target.database,
            isolation_level=settings.source_sql_isolation_level,
        )

    def read_baseline(self, request: BaselineReadRequest) -> BaselineReadResult:
        try:
            read = self._executor.read_baseline(request.store_code)
            source_watermark = watermark(read.source_watermark)
        except LegacyBaselineReadError:
            raise
        except Exception as error:  # noqa: BLE001 - driver errors need safe classification
            raise LegacyBaselineReadError(failure_signal(error)) from None

        return BaselineReadResult(
            rows=tuple(read.rows),
            provenance=self._provenance(request.source_window, source_watermark),
        )

    def _provenance(
        self, source_window: SourceWindow, source_watermark: datetime
    ) -> WarehouseProvenance:
        return WarehouseProvenance(
            instance=self._instance,
            database=self._database,
            objects=(_OBJECT,),
            query_version=BASELINE_QUERY_VERSION,
            source_window_start=source_window.start,
            source_window_end=source_window.end,
            source_watermark=source_watermark,
            isolation_level=self._isolation_level,
        )
