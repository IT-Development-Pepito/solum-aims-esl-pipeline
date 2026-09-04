"""Read-only AIMS PostgreSQL compatibility reader (#24, FR-020, AD-003, AD-004).

This is the temporary first-cutover path: the label read model is taken from
the vendor's databases directly, behind the ``AimsReadModelReader`` port from
#22, until a supported API replaces it. Retiring it means swapping this class
for another implementation of the same port; nothing above the port changes.

Two facts from the local clone shape the read. The store code lives only in
Portal (``end_device.station_code``) and the displayed page lives only in Core
(``enddevice.pages`` as JSON ``currentPage``), so a label is a join across two
databases and neither side can answer alone. And ``currentPage = -1`` marks a
device with no assignment: it is not a page number, and returning it as one
would misreport thousands of devices. Such devices are excluded from the
result and counted in the evidence of every read, so nothing is dropped
silently.

Two guards keep this read-only regardless of what the role was granted. The
session is opened with ``default_transaction_read_only=on``, so a write is
refused by PostgreSQL before permissions are consulted, and the class has no
method that could issue one.

Nothing here decides a business rule. Vendor data is reported as it is, the
malformed store code included, because a read-only adapter does not repair
the system it reads.
"""

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from esl_service.application.contracts import AimsLabel
from esl_service.config import Settings
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal
from esl_service.domain.serialization import JSONValue
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.runtime.connectivity import targets_from_settings
from esl_service.runtime.secrets import SecretProvider

#: Versioned so a later reader can tell which evidence shape it is looking at.
AIMS_READ_SCHEMA_VERSION = "aims-compatibility-read-v1"
AIMS_READ_ACTION = "aims.read"
AIMS_READ_RESOURCE = "aims_compatibility"

#: The two relations this reader depends on, and the columns it needs. A
#: failing probe on either is schema drift, classified rather than raised raw.
_PORTAL_SCHEMA_PROBE = "SELECT label_code, station_code FROM public.end_device WHERE false"
_CORE_SCHEMA_PROBE = "SELECT code, pages FROM public.enddevice WHERE false"

_PORTAL_LABELS = text(
    "SELECT label_code, station_code FROM public.end_device WHERE station_code = :store"
)
_CORE_DEVICES = text("SELECT code, pages FROM public.enddevice WHERE code = ANY(:codes)")


class AimsSchemaDrift(RuntimeError):
    """A relation or column this reader relies on is missing or changed.

    Carries the documented failure signal so the caller classifies it through
    the section 8 matrix instead of guessing from a driver message.
    """

    def __init__(self, relation: str) -> None:
        super().__init__(
            f"AIMS compatibility schema drift: relation {relation!r} is missing or changed"
        )
        self.relation = relation
        self.signal = FailureSignal(
            dependency=DependencyKind.AIMS_COMPATIBILITY, kind=FailureKind.SCHEMA_DRIFT
        )


class AimsUnavailable(RuntimeError):
    """The compatibility database did not answer, or answered with a fault.

    Carries the section 8 UNAVAILABLE signal, which is retryable, so an outage
    or a refused connection is never mistaken for a permanent schema change.
    """

    def __init__(self, relation: str) -> None:
        super().__init__(
            f"AIMS compatibility database unavailable while reading relation {relation!r}"
        )
        self.relation = relation
        self.signal = FailureSignal(
            dependency=DependencyKind.AIMS_COMPATIBILITY, kind=FailureKind.UNAVAILABLE
        )


#: PostgreSQL SQLSTATEs that mean a relation or column this reader relies on is
#: gone: undefined_table, undefined_column, invalid_schema_name. Every other
#: driver error, including one raised before a connection exists, is an
#: availability fault.
_SCHEMA_DRIFT_SQLSTATES = frozenset({"42P01", "42703", "3F000"})


def failure_for(error: BaseException, relation: str) -> AimsSchemaDrift | AimsUnavailable:
    """Map a driver error onto its section 8 row by SQLSTATE, never by its text.

    SQLAlchemy wraps the driver error as ``orig`` and psycopg carries the
    SQLSTATE as ``sqlstate``; the chain is walked so the answer does not
    depend on which layer raised. The driver message can embed the connection
    string, so it is dropped: the result carries only the relation name.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        if getattr(current, "sqlstate", None) in _SCHEMA_DRIFT_SQLSTATES:
            return AimsSchemaDrift(relation)
        current = getattr(current, "orig", None) or current.__cause__
    return AimsUnavailable(relation)


# --- the page field -----------------------------------------------------------


def _classify_pages(pages: str | None) -> tuple[str, int | None]:
    """Return ("page", n), ("unassigned", None), or ("malformed", None).

    The column is JSON inside a varchar. Only a non-negative integer
    ``currentPage`` is a page; ``-1`` is the vendor's sentinel for a device
    with no assignment; anything else is content this reader does not
    understand and will not guess at.
    """

    if not isinstance(pages, str) or not pages.strip():
        return "malformed", None
    try:
        decoded = json.loads(pages)
    except ValueError:
        return "malformed", None
    if not isinstance(decoded, dict):
        return "malformed", None
    current = decoded.get("currentPage")
    if isinstance(current, bool) or not isinstance(current, int):
        return "malformed", None
    if current == -1:
        return "unassigned", None
    if current < 0:
        return "malformed", None
    return "page", current


def parse_current_page(pages: str | None) -> int | None:
    """Return the displayed page, or None when there is none to report."""

    return _classify_pages(pages)[1]


# --- rows and the merge ---------------------------------------------------------


@dataclass(frozen=True)
class PortalLabel:
    label_code: str
    station_code: str


@dataclass(frozen=True)
class CoreDevice:
    code: str
    pages: str | None


@dataclass(frozen=True)
class MergedLabels:
    """What a read produced, and what it deliberately left out."""

    labels: tuple[AimsLabel, ...]
    unassigned: int
    malformed: int
    missing_in_core: int


def merge_labels(
    store_code: str, portal_rows: Iterable[PortalLabel], core_rows: Iterable[CoreDevice]
) -> MergedLabels:
    """Join Portal's labels for one store with Core's displayed pages.

    Only a label that exists in both and carries a real page becomes an
    ``AimsLabel``. Every exclusion is counted rather than dropped, and the
    result is ordered by label code so two reads of the same state compare
    equal.
    """

    pages_by_code = {device.code: device.pages for device in core_rows}
    labels: list[AimsLabel] = []
    unassigned = malformed = missing = 0

    for row in sorted(portal_rows, key=lambda item: item.label_code):
        if row.label_code not in pages_by_code:
            missing += 1
            continue
        kind, page = _classify_pages(pages_by_code[row.label_code])
        if kind == "unassigned":
            unassigned += 1
        elif kind == "malformed" or page is None:
            malformed += 1
        else:
            labels.append(AimsLabel(row.label_code, row.station_code, page))

    return MergedLabels(tuple(labels), unassigned, malformed, missing)


# --- evidence of every read (FR-022) --------------------------------------------


@dataclass(frozen=True)
class ReadEvidence:
    """Counts only. Never a host, a URL, or a value from the vendor's rows."""

    store_code: str
    portal_rows: int
    core_rows: int
    labels: int
    unassigned: int
    malformed: int
    missing_in_core: int
    duration_ms: int

    def as_evidence(self) -> dict[str, JSONValue]:
        return {
            "schema_version": AIMS_READ_SCHEMA_VERSION,
            "store_code": self.store_code,
            "portal_rows": self.portal_rows,
            "core_rows": self.core_rows,
            "labels": self.labels,
            "unassigned": self.unassigned,
            "malformed": self.malformed,
            "missing_in_core": self.missing_in_core,
            "duration_ms": self.duration_ms,
        }


class ReadAuditSink(Protocol):
    """Receives the evidence of one read."""

    def record(self, evidence: ReadEvidence) -> None: ...


class NoAuditSink:
    """For diagnostics that have no state store to write to."""

    def record(self, evidence: ReadEvidence) -> None:
        return None


class AuditedReadSink:
    """Appends one audit entry per read, with no execution, as FR-020 requires."""

    def __init__(self, repository: ReconciliationRepository, *, actor: str) -> None:
        self._repository = repository
        self._actor = actor

    def record(self, evidence: ReadEvidence) -> None:
        self._repository.append_audit_entry(
            actor=self._actor,
            action=AIMS_READ_ACTION,
            reason="AIMS compatibility read",
            resource_type=AIMS_READ_RESOURCE,
            resource_key=evidence.store_code,
            outcome="READ",
            after_evidence=evidence.as_evidence(),
        )


# --- the reader -----------------------------------------------------------------


#: Matches ``Settings.aims_connect_timeout_seconds``; tests build engines directly.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


def create_read_only_engine(
    url: URL, *, connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
) -> Engine:
    """An engine whose every session is read-only at the server, not by policy.

    ``default_transaction_read_only=on`` makes PostgreSQL refuse a write before
    it consults the role's grants, so the least-privilege identity is a second
    line rather than the only one. The connect timeout (#112) travels as a
    driver argument, never in the URL, so a logged URL cannot carry it and a
    host that drops packets fails fast into the retry policy instead of
    stalling until TCP gives up.
    """

    if connect_timeout_seconds < 1:
        raise ValueError("connect_timeout_seconds must be at least one second")
    connect_args: dict[str, object] = {"options": "-c default_transaction_read_only=on"}
    # SQLAlchemy merges connect_args over URL query parameters, so a timeout an
    # operator or a test put in the URL must not be silently replaced.
    if "connect_timeout" not in url.query:
        connect_args["connect_timeout"] = connect_timeout_seconds
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


class AimsCompatibilityReader:
    """Implements ``AimsReadModelReader`` over the two AIMS databases."""

    def __init__(
        self,
        portal_engine: Engine,
        core_engine: Engine,
        *,
        sink: ReadAuditSink | None = None,
        chunk_size: int = 1000,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self._portal = portal_engine
        self._core = core_engine
        self._sink: ReadAuditSink = sink or NoAuditSink()
        self._chunk_size = chunk_size

    @classmethod
    def from_settings(
        cls, settings: Settings, secrets: SecretProvider, *, sink: ReadAuditSink | None = None
    ) -> "AimsCompatibilityReader":
        """Build from configuration, with both passwords taken from the bundle."""

        targets = {target.name: target for target in targets_from_settings(settings)}
        engines: list[Engine] = []
        for name in ("aims-portal", "aims-core"):
            target = targets[name]
            if not target.configured():
                raise ValueError(f"AIMS target {name!r} is not configured")
            engines.append(
                create_read_only_engine(
                    target.sqlalchemy_url(secrets.get(target.password_key)),
                    connect_timeout_seconds=settings.aims_connect_timeout_seconds,
                )
            )
        return cls(engines[0], engines[1], sink=sink)

    def fetch_labels(self, store_code: str) -> Sequence[AimsLabel]:
        """Return the labels AIMS currently reports for one store.

        Unassigned devices, malformed page data, and labels Portal knows but
        Core does not are excluded from the result and counted in the audit
        evidence of the read.
        """

        started = time.perf_counter()
        self._verify_schema()

        portal_rows = self._portal_labels(store_code)
        core_rows = self._core_devices([row.label_code for row in portal_rows])
        merged = merge_labels(store_code, portal_rows, core_rows)

        self._sink.record(
            ReadEvidence(
                store_code=store_code,
                portal_rows=len(portal_rows),
                core_rows=len(core_rows),
                labels=len(merged.labels),
                unassigned=merged.unassigned,
                malformed=merged.malformed,
                missing_in_core=merged.missing_in_core,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        return merged.labels

    def _verify_schema(self) -> None:
        for engine, relation, probe in (
            (self._portal, "end_device", _PORTAL_SCHEMA_PROBE),
            (self._core, "enddevice", _CORE_SCHEMA_PROBE),
        ):
            try:
                with engine.connect() as connection:
                    connection.execute(text(probe))
            except DBAPIError as error:
                # The driver message can embed the connection string.
                raise failure_for(error, relation) from None

    def _portal_labels(self, store_code: str) -> list[PortalLabel]:
        try:
            with self._portal.connect() as connection:
                rows = connection.execute(_PORTAL_LABELS, {"store": store_code}).all()
        except DBAPIError as error:
            raise failure_for(error, "end_device") from None
        return [PortalLabel(str(row[0]), str(row[1])) for row in rows]

    def _core_devices(self, codes: Sequence[str]) -> list[CoreDevice]:
        devices: list[CoreDevice] = []
        try:
            with self._core.connect() as connection:
                for start in range(0, len(codes), self._chunk_size):
                    chunk = list(codes[start : start + self._chunk_size])
                    rows = connection.execute(_CORE_DEVICES, {"codes": chunk}).all()
                    devices.extend(
                        CoreDevice(str(row[0]), None if row[1] is None else str(row[1]))
                        for row in rows
                    )
        except DBAPIError as error:
            raise failure_for(error, "enddevice") from None
        return devices
