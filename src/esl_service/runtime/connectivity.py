"""Connectivity to every configured database (#78, #79, NFR-009, AD-017).

Targets are derived from configuration, which names where each database is
and as whom to connect. Passwords are never part of a target; each target
names a fixed bundle key and the value is read at the moment of connecting.

A check answers one question per target -- can we reach it, and as whom --
and never says how it tried. Driver error messages routinely embed the whole
connection string, so no exception text ever reaches a result; the outcome
vocabulary carries the diagnosis instead, and it is deliberately five-valued
so a developer can tell a credential fault from a route fault from a missing
secret from a target nobody has configured yet.

The same probe serves two callers: an administrator running
``esl-admin check-connections`` by hand, and ``HealthService`` reporting
dependency health. Only the caller differs.

Per-store targets are the one case where a connection address comes from
table data rather than configuration: ``DimStore.ORG_IP`` and ``ORG_DB``.
They are validated as a bare IP or hostname before use, because anyone who
can write that row could otherwise redirect a connection.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from esl_service.config import Settings
from esl_service.runtime.health import DependencyHealth, HealthState
from esl_service.runtime.secrets import (
    AIMS_CORE_PASSWORD_KEY,
    AIMS_PORTAL_PASSWORD_KEY,
    SOURCE_SQL_PASSWORD_KEY,
    STATE_PASSWORD_KEY,
    SecretProvider,
    SecretUnavailableError,
)

__all__ = [
    "AIMS_CORE_PASSWORD_KEY",
    "AIMS_PORTAL_PASSWORD_KEY",
    "SOURCE_SQL_PASSWORD_KEY",
    "STATE_PASSWORD_KEY",
    "ConnectionTarget",
    "ConnectivityProbe",
    "Connector",
    "InvalidStoreAddress",
    "ProbeOutcome",
    "ProbeResult",
    "SqlAlchemyConnector",
    "TargetKind",
    "build_probes",
    "classify_failure",
    "parse_target",
    "probe",
    "state_store_target",
    "store_target",
    "targets_from_settings",
]


class TargetKind(StrEnum):
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"


_DRIVERS = {TargetKind.POSTGRESQL: "postgresql+psycopg", TargetKind.SQLSERVER: "mssql+pyodbc"}
_IDENTITY_QUERY = {
    TargetKind.POSTGRESQL: "SELECT current_user",
    TargetKind.SQLSERVER: "SELECT SUSER_SNAME()",
}
#: Used when a target is built without configuration, e.g. from --target.
DEFAULT_SQL_SERVER_DRIVER = "ODBC Driver 18 for SQL Server"

#: SQLSTATE values that mean the server answered and refused the credential.
_CREDENTIAL_SQLSTATES = frozenset({"28P01", "28000"})
_CREDENTIAL_PHRASES = ("password authentication failed", "login failed")

#: A bare IPv4 address, or a hostname of dot-separated labels. Nothing else.
_IPV4 = re.compile(r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class ProbeOutcome(StrEnum):
    REACHABLE = "REACHABLE"
    UNREACHABLE = "UNREACHABLE"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    UNCONFIGURED = "UNCONFIGURED"
    #: The ODBC driver named in configuration is not installed. Raised before
    #: any network activity, so it is not a route fault and is reported apart.
    DRIVER_MISSING = "DRIVER_MISSING"


#: Fixed, safe wording per outcome. Never the exception text, and never a
#: word the health report treats as secret-like.
_DETAIL = {
    ProbeOutcome.UNREACHABLE: "no answer from the host, port, or database",
    ProbeOutcome.CREDENTIAL_REJECTED: "the server answered and refused the login",
    ProbeOutcome.SECRET_UNAVAILABLE: "the bundle entry for this target is not provisioned",
    ProbeOutcome.UNCONFIGURED: "host, database, or account is not configured",
}


class InvalidStoreAddress(ValueError):
    """Raised when a DimStore row does not hold a plain address or database name."""


@dataclass(frozen=True)
class ConnectionTarget:
    """One database to reach. The password is a bundle key, never a value."""

    name: str
    kind: TargetKind
    host: str
    port: int | None
    database: str
    username: str
    password_key: str
    #: Only for targets whose configured URL already embeds the password.
    password: str | None = field(default=None, repr=False)
    driver: str = DEFAULT_SQL_SERVER_DRIVER
    trust_server_certificate: bool = True

    def configured(self) -> bool:
        return all(part.strip() for part in (self.host, self.database, self.username))

    def sqlalchemy_url(self, password: str) -> URL:
        """Build the URL from parts, so a password never needs escaping."""

        query = (
            {
                "driver": self.driver,
                "TrustServerCertificate": "yes" if self.trust_server_certificate else "no",
            }
            if self.kind is TargetKind.SQLSERVER
            else {}
        )
        return URL.create(
            _DRIVERS[self.kind],
            username=self.username,
            password=password,
            host=self.host,
            port=self.port,
            database=self.database,
            query=query,
        )


@dataclass(frozen=True)
class ProbeResult:
    name: str
    outcome: ProbeOutcome
    identity: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (ProbeOutcome.REACHABLE, ProbeOutcome.UNCONFIGURED)


class Connector(Protocol):
    """Opens one connection and returns the identity it connected as."""

    def connect_and_identify(self, url: URL) -> str: ...


class SqlAlchemyConnector:
    """Real connections through SQLAlchemy, with a bounded connect timeout."""

    def __init__(self, timeout_seconds: int = 10) -> None:
        self._timeout = timeout_seconds

    def connect_and_identify(self, url: URL) -> str:
        if url.drivername.startswith("postgresql"):
            connect_args: dict[str, int] = {"connect_timeout": self._timeout}
            query = _IDENTITY_QUERY[TargetKind.POSTGRESQL]
        else:
            connect_args = {"timeout": self._timeout}
            query = _IDENTITY_QUERY[TargetKind.SQLSERVER]

        engine = create_engine(url, connect_args=connect_args)
        try:
            with engine.connect() as connection:
                return str(connection.execute(text(query)).scalar_one())
        finally:
            engine.dispose()


def classify_failure(error: BaseException) -> ProbeOutcome:
    """Decide whether the server refused the credential or never answered.

    SQLAlchemy wraps the driver error and exposes it as ``orig``; psycopg
    carries ``sqlstate``; pyodbc puts the SQLSTATE in ``args[0]``. All three
    shapes are checked, plus the two phrases the servers use, so the answer
    does not depend on which layer raised.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        sqlstate = getattr(current, "sqlstate", None)
        first_arg = current.args[0] if current.args else None
        message = str(current).casefold()
        # ODBC's "no such driver": pyodbc puts IM002 in args[0] and in the text.
        if sqlstate == "IM002" or first_arg == "IM002" or "[im002]" in message:
            return ProbeOutcome.DRIVER_MISSING
        if (
            sqlstate in _CREDENTIAL_SQLSTATES
            or first_arg in _CREDENTIAL_SQLSTATES
            or any(phrase in message for phrase in _CREDENTIAL_PHRASES)
        ):
            return ProbeOutcome.CREDENTIAL_REJECTED
        current = getattr(current, "orig", None) or current.__cause__
    return ProbeOutcome.UNREACHABLE


def probe(target: ConnectionTarget, secrets: SecretProvider, connector: Connector) -> ProbeResult:
    """Attempt one target and report an outcome that discloses nothing."""

    if not target.configured():
        return ProbeResult(
            target.name, ProbeOutcome.UNCONFIGURED, detail=_DETAIL[ProbeOutcome.UNCONFIGURED]
        )

    try:
        password = target.password or secrets.get(target.password_key)
    except SecretUnavailableError:
        return ProbeResult(
            target.name,
            ProbeOutcome.SECRET_UNAVAILABLE,
            detail=_DETAIL[ProbeOutcome.SECRET_UNAVAILABLE],
        )

    try:
        identity = connector.connect_and_identify(target.sqlalchemy_url(password))
    # Any failure is classified into the fixed vocabulary; its text is dropped
    # because it commonly contains the connection string.
    except Exception as error:  # noqa: BLE001
        outcome = classify_failure(error)
        if outcome is ProbeOutcome.DRIVER_MISSING:
            # The name is the diagnosis: a stray '+' or a typo shows at once.
            detail = f"ODBC driver {target.driver!r} is not installed on this machine"
        else:
            detail = _DETAIL[outcome]
        return ProbeResult(target.name, outcome, detail=detail)
    return ProbeResult(target.name, ProbeOutcome.REACHABLE, identity=identity)


# --- health integration (#78) ----------------------------------------------

_HEALTH_STATE = {
    ProbeOutcome.REACHABLE: HealthState.HEALTHY,
    ProbeOutcome.UNCONFIGURED: HealthState.DEGRADED,
    ProbeOutcome.UNREACHABLE: HealthState.UNAVAILABLE,
    ProbeOutcome.CREDENTIAL_REJECTED: HealthState.UNAVAILABLE,
    ProbeOutcome.SECRET_UNAVAILABLE: HealthState.UNAVAILABLE,
    ProbeOutcome.DRIVER_MISSING: HealthState.UNAVAILABLE,
}


class ConnectivityProbe:
    """One target reported through ``HealthService`` (FR-024).

    Only the state store is required: a source that is down degrades the
    report but must not make a running service report itself unready, since
    it can still serve status and audit. An unconfigured target is degraded
    rather than unavailable, so a gap in configuration is visible without
    being counted as an outage.
    """

    def __init__(
        self,
        target: ConnectionTarget,
        secrets: SecretProvider,
        connector: Connector,
        *,
        required: bool = False,
    ) -> None:
        self._target = target
        self._secrets = secrets
        self._connector = connector
        self.name = target.name
        self.required = required

    def check(self) -> DependencyHealth:
        result = probe(self._target, self._secrets, self._connector)
        return DependencyHealth(
            name=self.name,
            state=_HEALTH_STATE[result.outcome],
            required=self.required,
            detail=result.detail,
        )


def build_probes(
    settings: Settings, secrets: SecretProvider, connector: Connector
) -> tuple[ConnectivityProbe, ...]:
    """One probe per configured target; only the state store is required."""

    return tuple(
        ConnectivityProbe(
            target, secrets, connector, required=target.name == STATE_STORE_NAME
        )
        for target in targets_from_settings(settings)
    )


# --- targets from configuration (#78) --------------------------------------

STATE_STORE_NAME = "state-store"


def state_store_target(database_url: str) -> ConnectionTarget:
    """The service's own PostgreSQL. Its password comes from the bundle.

    ``ESL_DATABASE_URL`` names where and as whom; any password it still
    embeds is ignored here and refused by the startup gate, so the bundle is
    the single source of truth for that credential.
    """

    url = make_url(database_url)
    return ConnectionTarget(
        name=STATE_STORE_NAME,
        kind=TargetKind.POSTGRESQL,
        host=url.host or "",
        port=url.port,
        database=url.database or "",
        username=url.username or "",
        password_key=STATE_PASSWORD_KEY,
    )


def _sql_server(settings: Settings, name: str, host: str, database: str) -> ConnectionTarget:
    return ConnectionTarget(
        name=name,
        kind=TargetKind.SQLSERVER,
        host=host,
        port=None,
        database=database,
        username=settings.source_sql_username,
        password_key=SOURCE_SQL_PASSWORD_KEY,
        driver=settings.source_sql_driver,
        trust_server_certificate=settings.source_sql_trust_server_certificate,
    )


def _aims(settings: Settings, name: str, database: str, username: str, key: str) -> ConnectionTarget:
    return ConnectionTarget(
        name=name,
        kind=TargetKind.POSTGRESQL,
        host=settings.aims_host,
        port=settings.aims_port,
        database=database,
        username=username,
        password_key=key,
    )


def targets_from_settings(settings: Settings) -> tuple[ConnectionTarget, ...]:
    """Every tier configuration knows about, configured or not.

    Unconfigured tiers are returned rather than omitted, so a report shows
    the gap while access is still being arranged. ``STORE_OPS_APP`` is
    deliberately absent: its only use in the procedure is commented out.
    Per-store servers are not here either, because their addresses come from
    ``DimStore`` at run time; see :func:`store_target`.
    """

    return (
        state_store_target(settings.database_url),
        _sql_server(settings, "warehouse", settings.source_sql_host, settings.source_warehouse_database),
        _sql_server(settings, "legacy-baseline", settings.source_sql_host, settings.legacy_baseline_database),
        _sql_server(settings, "pepito-ho", settings.source_pepito_ho_host, settings.source_pepito_ho_database),
        _aims(settings, "aims-portal", settings.aims_portal_database, settings.aims_portal_username, AIMS_PORTAL_PASSWORD_KEY),
        _aims(settings, "aims-core", settings.aims_core_database, settings.aims_core_username, AIMS_CORE_PASSWORD_KEY),
    )


def store_target(settings: Settings, *, store_code: str, org_ip: str, org_db: str) -> ConnectionTarget:
    """A per-store iRetail server, addressed from a ``DimStore`` row.

    The address is validated as a bare IPv4 address or hostname and the
    database as a plain identifier before either is used. ``ORG_IP`` holds a
    bare IP with no port, confirmed by the source owner, so no port is
    accepted or assumed.
    """

    address = org_ip.strip()
    if not (_IPV4.match(address) or _HOSTNAME.match(address)):
        raise InvalidStoreAddress(
            f"store {store_code!r} address must be a bare IP address or hostname"
        )
    database = org_db.strip()
    if not _IDENTIFIER.match(database):
        raise InvalidStoreAddress(
            f"store {store_code!r} database name must be a plain identifier"
        )
    return _sql_server(settings, f"store-{store_code.strip()}", address, database)


# --- targets from the command line (#79) -----------------------------------


def parse_target(spec: str) -> ConnectionTarget:
    """Parse ``name=kind://user@host:port/db#password.key`` from the command line.

    The fragment is the bundle key, so a password can never be typed into a
    shell where it would reach history or a process listing.
    """

    name, separator, remainder = spec.partition("=")
    if not separator or not name.strip():
        raise ValueError("a target is written as name=kind://user@host:port/db#password.key")
    location, hash_sign, password_key = remainder.partition("#")
    if not hash_sign or not password_key.strip():
        raise ValueError("a target needs a password key after '#'")

    url = make_url(location)
    if url.password:
        raise ValueError("a target must not carry an inline password; name a bundle key")
    scheme = url.drivername.split("+")[0]
    kinds = {
        "postgresql": TargetKind.POSTGRESQL,
        "mssql": TargetKind.SQLSERVER,
        "sqlserver": TargetKind.SQLSERVER,
    }
    if scheme not in kinds:
        raise ValueError(f"unsupported target kind {scheme!r}; use postgresql or sqlserver")

    return ConnectionTarget(
        name=name.strip(),
        kind=kinds[scheme],
        host=url.host or "",
        port=url.port,
        database=url.database or "",
        username=url.username or "",
        password_key=password_key.strip(),
    )
