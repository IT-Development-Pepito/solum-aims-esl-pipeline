"""On-demand connectivity checks for every configured database (#79, NFR-009).

A check answers one question per target -- can we reach it, and as whom --
and never says how it tried. Driver error messages routinely embed the whole
connection string, so no exception text ever reaches a result; the outcome
vocabulary carries the diagnosis instead, and it is deliberately five-valued
so a developer can tell a credential fault from a route fault from a missing
secret from a target nobody has configured yet.

This is the same probe #78 later wires into ``HealthService``. Only the caller
differs: here it is an administrator running a command by hand.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from esl_service.runtime.secrets import SecretProvider, SecretUnavailableError


class TargetKind(StrEnum):
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"


_DRIVERS = {TargetKind.POSTGRESQL: "postgresql+psycopg", TargetKind.SQLSERVER: "mssql+pyodbc"}
_IDENTITY_QUERY = {
    TargetKind.POSTGRESQL: "SELECT current_user",
    TargetKind.SQLSERVER: "SELECT SUSER_SNAME()",
}
#: SQL Server needs the ODBC driver named in the URL; #78 makes it configurable.
DEFAULT_SQL_SERVER_DRIVER = "ODBC Driver 18 for SQL Server"

#: SQLSTATE values that mean the server answered and refused the credential.
_CREDENTIAL_SQLSTATES = frozenset({"28P01", "28000"})
_CREDENTIAL_PHRASES = ("password authentication failed", "login failed")


class ProbeOutcome(StrEnum):
    REACHABLE = "REACHABLE"
    UNREACHABLE = "UNREACHABLE"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    UNCONFIGURED = "UNCONFIGURED"


#: Fixed, safe wording per outcome. Never the exception text.
_DETAIL = {
    ProbeOutcome.UNREACHABLE: "no answer from the host, port, or database",
    ProbeOutcome.CREDENTIAL_REJECTED: "the server answered and refused the credential",
    ProbeOutcome.SECRET_UNAVAILABLE: "the password key is not in the secret bundle",
    ProbeOutcome.UNCONFIGURED: "host, database, or username is not configured",
}


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

    def configured(self) -> bool:
        return all(part.strip() for part in (self.host, self.database, self.username))

    def sqlalchemy_url(self, password: str) -> URL:
        """Build the URL from parts, so a password never needs escaping."""

        query = (
            {"driver": DEFAULT_SQL_SERVER_DRIVER, "TrustServerCertificate": "yes"}
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
        return ProbeResult(target.name, ProbeOutcome.UNCONFIGURED, detail=_DETAIL[ProbeOutcome.UNCONFIGURED])

    try:
        password = target.password or secrets.get(target.password_key)
    except SecretUnavailableError:
        return ProbeResult(
            target.name, ProbeOutcome.SECRET_UNAVAILABLE, detail=_DETAIL[ProbeOutcome.SECRET_UNAVAILABLE]
        )

    try:
        identity = connector.connect_and_identify(target.sqlalchemy_url(password))
    # Any failure is classified into the fixed vocabulary; its text is dropped
    # because it commonly contains the connection string.
    except Exception as error:  # noqa: BLE001
        outcome = classify_failure(error)
        return ProbeResult(target.name, outcome, detail=_DETAIL[outcome])
    return ProbeResult(target.name, ProbeOutcome.REACHABLE, identity=identity)


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
    kinds = {"postgresql": TargetKind.POSTGRESQL, "mssql": TargetKind.SQLSERVER, "sqlserver": TargetKind.SQLSERVER}
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


def state_store_target(database_url: str) -> ConnectionTarget:
    """The service's own PostgreSQL, whose configured URL embeds its password."""

    url = make_url(database_url)
    return ConnectionTarget(
        name="state-store",
        kind=TargetKind.POSTGRESQL,
        host=url.host or "",
        port=url.port,
        database=url.database or "",
        username=url.username or "",
        password_key="state.password",
        password=url.password,
    )
