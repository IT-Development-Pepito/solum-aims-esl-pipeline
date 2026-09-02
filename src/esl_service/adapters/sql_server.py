"""Shared SQL Server transport helpers for the source-tier adapters (#91, #93).

Every SQL Server tier (`DBWH_8555`, `PEPITO_HO`, the per-store iRetail
servers) is read the same way: a read-intent connection, transaction-level
``SNAPSHOT`` isolation with no weaker fallback, a database-side UTC read
timestamp as the watermark, and driver failures translated into the #20
failure signals without ever surfacing driver text, which commonly embeds a
connection string. Those rules live here once so the adapters cannot drift
apart, and so a new tier adds only its own closed SELECTs.
"""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.runtime.connectivity import ProbeOutcome, classify_failure

#: SQLSTATE classes that mean the schema is not what the query expects.
SCHEMA_SQLSTATE_PREFIXES = ("42",)

#: The database's own clock, so the watermark is the source's time, not ours.
READ_TIME_SQL = "SELECT SYSUTCDATETIME() AS source_watermark"

#: AD-020. A live read on 2026-09-02 found snapshot isolation OFF on all three
#: source databases (PEPITO_HO, DBWH_8555, ESL), so SNAPSHOT-only reads fail
#: with error 3952. READ COMMITTED is SQL Server's default and stricter than
#: the legacy procedure's NOLOCK; SNAPSHOT is selected by configuration once a
#: DBA enables it. The level in use is recorded in every read's provenance.
DEFAULT_ISOLATION_LEVEL = "READ COMMITTED"
SUPPORTED_ISOLATION_LEVELS = ("READ COMMITTED", "SNAPSHOT")


def build_read_only_url(url: URL) -> URL:
    """Request SQL Server read intent without changing or rendering credentials."""

    return url.update_query_dict({"ApplicationIntent": "ReadOnly"})


def create_read_only_engine(
    url: URL, *, isolation_level: str = DEFAULT_ISOLATION_LEVEL
) -> Engine:
    """Create a source-tier engine: read intent, the configured isolation, bounded connect."""

    if isolation_level not in SUPPORTED_ISOLATION_LEVELS:
        raise ValueError(
            f"unsupported SQL Server isolation level {isolation_level!r}; "
            f"choose one of {', '.join(SUPPORTED_ISOLATION_LEVELS)}"
        )
    return create_engine(
        build_read_only_url(url),
        connect_args={"timeout": 10},
        isolation_level=isolation_level,
        pool_pre_ping=True,
    )


def walk_errors(error: BaseException) -> Iterator[BaseException]:
    """Yield an exception and the driver errors it wraps, without cycling."""

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        yield current
        current = getattr(current, "orig", None) or current.__cause__


def is_schema_drift(error: BaseException) -> bool:
    for current in walk_errors(error):
        sqlstate = getattr(current, "sqlstate", None)
        first_arg = current.args[0] if current.args else None
        for candidate in (sqlstate, first_arg):
            if isinstance(candidate, str) and candidate.startswith(SCHEMA_SQLSTATE_PREFIXES):
                return True
    return False


#: ODBC timeout SQLSTATEs: query timeout expired, connection timeout expired.
TIMEOUT_SQLSTATES = ("HYT00", "HYT01")


def is_timeout(error: BaseException) -> bool:
    for current in walk_errors(error):
        sqlstate = getattr(current, "sqlstate", None)
        first_arg = current.args[0] if current.args else None
        if any(candidate in TIMEOUT_SQLSTATES for candidate in (sqlstate, first_arg)):
            return True
    return False


def failure_signal(error: BaseException) -> FailureSignal:
    """Map a driver or shape failure onto an existing #20 signal, never its text."""

    if is_timeout(error):
        return FailureSignal(DependencyKind.SQL_SERVER, FailureKind.TIMEOUT)
    if is_schema_drift(error) or isinstance(error, KeyError | TypeError | ValueError):
        return FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED)

    outcome = classify_failure(error)
    if outcome is ProbeOutcome.CREDENTIAL_REJECTED:
        return FailureSignal(DependencyKind.CREDENTIAL, FailureKind.EXPIRED)
    if outcome is ProbeOutcome.DRIVER_MISSING:
        return FailureSignal(DependencyKind.CONFIGURATION, FailureKind.MALFORMED)
    return FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE)


def watermark(value: object) -> datetime:
    """Normalise the database read time to an aware UTC instant."""

    if not isinstance(value, datetime):
        raise TypeError("source watermark is not a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def source_text(row: Mapping[str, object], column: str) -> str:
    """Return one required, non-blank text column, trimmed."""

    value = row[column]
    if not isinstance(value, str):
        raise TypeError(f"{column} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{column} must not be blank")
    return normalized
