"""The read-only AIMS compatibility reader, without a database (#24, FR-020).

What this adapter decides is small and must be exact: which Core device
rows become labels, which are excluded and why, and what it records about
every read. Those decisions are pure functions here so they are testable
without either AIMS database. The join itself, the read-only session, and
the least-privilege proof are integration tests against the local clone.
"""

import inspect
import socket
from typing import Self

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from esl_service.adapters.aims_compatibility import (
    AIMS_READ_ACTION,
    AIMS_READ_SCHEMA_VERSION,
    AimsCompatibilityReader,
    AimsSchemaDrift,
    AimsUnavailable,
    CoreDevice,
    PortalLabel,
    ReadEvidence,
    create_read_only_engine,
    failure_for,
    merge_labels,
    parse_current_page,
)
from esl_service.application.contracts import AimsLabel, AimsReadModelReader
from esl_service.domain.failures import (
    DependencyKind,
    FailureClass,
    FailureKind,
    classify,
)

# --- the page is a JSON field inside a varchar, and -1 is a sentinel -------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"currentPage":1,"returnPage":1,"exceptionPage":1}', 1),
        ('{"currentPage":4,"returnPage":1,"exceptionPage":1}', 4),
        ('{"currentPage":0,"returnPage":0,"exceptionPage":0}', 0),
        ('{"currentPage":-1,"returnPage":-1,"exceptionPage":-1}', None),
    ],
)
def test_current_page_is_read_from_the_json_and_minus_one_means_unassigned(
    text: str, expected: int | None
) -> None:
    assert parse_current_page(text) == expected


@pytest.mark.parametrize(
    "text",
    [None, "", "not json", "[]", '{"returnPage":1}', '{"currentPage":"1"}', '{"currentPage":-7}'],
)
def test_anything_else_is_not_a_page(text: str | None) -> None:
    """Malformed content is reported as such, never guessed into a page."""

    assert parse_current_page(text) is None


# --- merging portal and core: what becomes a label, what is counted --------


def portal(*codes: str, store: str = "084") -> list[PortalLabel]:
    return [PortalLabel(label_code=code, station_code=store) for code in codes]


def core(**pages: str) -> list[CoreDevice]:
    return [CoreDevice(code=code, pages=text) for code, text in pages.items()]


def test_an_assigned_device_becomes_a_label_with_the_store_and_current_page() -> None:
    merged = merge_labels(
        "084", portal("F5860001"), core(F5860001='{"currentPage":2,"returnPage":1,"exceptionPage":1}')
    )

    assert merged.labels == (AimsLabel("F5860001", "084", 2),)
    assert (merged.unassigned, merged.malformed, merged.missing_in_core) == (0, 0, 0)


def test_an_unassigned_device_is_excluded_and_counted() -> None:
    """The port has no way to say 'no page'; -1 must not masquerade as one."""

    merged = merge_labels(
        "084", portal("A", "B"),
        core(A='{"currentPage":1,"returnPage":1,"exceptionPage":1}',
             B='{"currentPage":-1,"returnPage":-1,"exceptionPage":-1}'),
    )

    assert [label.label_code for label in merged.labels] == ["A"]
    assert merged.unassigned == 1


def test_a_malformed_pages_value_is_excluded_and_counted() -> None:
    merged = merge_labels("084", portal("A"), core(A="not json"))

    assert merged.labels == ()
    assert merged.malformed == 1


def test_a_portal_label_with_no_core_device_is_counted_not_invented() -> None:
    """Portal and Core can disagree; the gap is evidence, not a page 0."""

    merged = merge_labels("084", portal("A"), core())

    assert merged.labels == ()
    assert merged.missing_in_core == 1


def test_core_devices_for_other_labels_are_ignored() -> None:
    """Core holds more devices than the store's labels; only the join counts."""

    merged = merge_labels(
        "084", portal("A"),
        core(A='{"currentPage":1,"returnPage":1,"exceptionPage":1}',
             Z='{"currentPage":3,"returnPage":1,"exceptionPage":1}'),
    )

    assert [label.label_code for label in merged.labels] == ["A"]


def test_labels_are_returned_in_a_deterministic_order() -> None:
    merged = merge_labels(
        "084", portal("B", "A"),
        core(A='{"currentPage":1,"returnPage":1,"exceptionPage":1}',
             B='{"currentPage":1,"returnPage":1,"exceptionPage":1}'),
    )

    assert [label.label_code for label in merged.labels] == ["A", "B"]


# --- every read is audit-visible and discloses nothing ----------------------


def test_read_evidence_carries_counts_and_never_a_location() -> None:
    evidence = ReadEvidence(
        store_code="084", portal_rows=10, core_rows=9, labels=8,
        unassigned=1, malformed=0, missing_in_core=1, duration_ms=12,
    ).as_evidence()

    assert evidence["schema_version"] == AIMS_READ_SCHEMA_VERSION
    assert evidence["store_code"] == "084"
    assert evidence["labels"] == 8
    assert evidence["unassigned"] == 1
    assert evidence["missing_in_core"] == 1
    joined = " ".join(f"{k}={v}" for k, v in evidence.items()).lower()
    assert "postgresql" not in joined and "password" not in joined and "://" not in joined


def test_the_audit_action_names_the_boundary() -> None:
    assert AIMS_READ_ACTION == "aims.read"


# --- the adapter is a read-only port implementation (acceptance criterion 3)


def test_the_reader_satisfies_the_application_port() -> None:
    """Retirement is swapping the implementation; the port stays."""

    assert AimsReadModelReader in AimsCompatibilityReader.__mro__ or isinstance(
        AimsCompatibilityReader.__new__(AimsCompatibilityReader), AimsReadModelReader
    )


def test_the_reader_exposes_no_write_method() -> None:
    """Names are a weak guard alone; the read-only session is the strong one."""

    mutating = ("insert", "update", "delete", "write", "set_", "change", "create", "drop", "truncate")
    public = [
        name for name, _ in inspect.getmembers(AimsCompatibilityReader, inspect.isfunction)
        if not name.startswith("_")
    ]

    assert public, "the reader must have public read methods"
    assert not [name for name in public if name.lower().startswith(mutating)]


# --- schema drift is a classified failure, not a stack trace -----------------


def test_schema_drift_maps_to_the_documented_failure_signal() -> None:
    """Architecture section 8: compatibility-DB schema drift is not retryable."""

    error = AimsSchemaDrift("end_device")

    assert error.signal.dependency is DependencyKind.AIMS_COMPATIBILITY
    assert error.signal.kind is FailureKind.SCHEMA_DRIFT
    assert "end_device" in str(error)
    assert "://" not in str(error)


# --- an unreachable database is unavailable, never drift (#110) ---------------


class _DriverError(Exception):
    """The shape psycopg gives a server or connection error: a ``sqlstate``."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("postgresql://reader:hunter2@aims.example/AIMS_PORTAL_DB refused")
        self.sqlstate = sqlstate


def _driver_error(sqlstate: str | None) -> OperationalError:
    return OperationalError("SELECT 1", {}, _DriverError(sqlstate))


def test_unavailable_maps_to_the_documented_failure_signal() -> None:
    """Architecture section 8: an unavailable compatibility DB is retryable."""

    error = AimsUnavailable("end_device")

    assert error.signal.dependency is DependencyKind.AIMS_COMPATIBILITY
    assert error.signal.kind is FailureKind.UNAVAILABLE
    assert classify(error.signal) is FailureClass.RETRYABLE
    assert "end_device" in str(error)
    assert "://" not in str(error)


@pytest.mark.parametrize("sqlstate", ["42P01", "42703", "3F000"])
def test_a_missing_relation_or_column_is_schema_drift(sqlstate: str) -> None:
    failure = failure_for(_driver_error(sqlstate), "enddevice")

    assert isinstance(failure, AimsSchemaDrift)
    assert failure.relation == "enddevice"


@pytest.mark.parametrize("sqlstate", [None, "08001", "08006", "57P01", "28P01", "42501"])
def test_any_other_driver_error_is_unavailable(sqlstate: str | None) -> None:
    failure = failure_for(_driver_error(sqlstate), "enddevice")

    assert isinstance(failure, AimsUnavailable)
    assert failure.relation == "enddevice"


def test_the_classified_failure_never_carries_the_driver_text() -> None:
    for sqlstate in ("42P01", None):
        failure = failure_for(_driver_error(sqlstate), "end_device")
        assert "hunter2" not in str(failure)
        assert "://" not in str(failure)
        assert failure.__cause__ is None


def test_a_refused_connection_on_the_probe_is_unavailable_not_drift() -> None:
    """Nothing listens on the port; that is an outage, not a changed relation.

    The port was bound and released a moment ago, so nothing answers. Some hosts
    drop rather than refuse, so the driver is given a short connect timeout.
    """

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dead = create_read_only_engine(
        make_url(f"postgresql+psycopg://reader:hunter2@127.0.0.1:{port}/AIMS?connect_timeout=2")
    )
    try:
        with pytest.raises(AimsUnavailable) as error:
            AimsCompatibilityReader(dead, dead).fetch_labels("084")
    finally:
        dead.dispose()

    assert error.value.signal.kind is FailureKind.UNAVAILABLE
    assert "hunter2" not in str(error.value)


class _Connection:
    def __init__(self, failing: str | None) -> None:
        self._failing = failing

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: object = None) -> Self:
        if self._failing is not None and self._failing in str(statement):
            raise _driver_error("08006")
        return self

    def all(self) -> list[tuple[str, str]]:
        return [("L1", "084")]


class _Engine:
    """Probes succeed; the statement containing ``failing`` raises like a dropped link."""

    def __init__(self, failing: str | None = None) -> None:
        self._failing = failing

    def connect(self) -> _Connection:
        return _Connection(self._failing)


def test_a_driver_error_while_reading_portal_labels_is_classified() -> None:
    reader = AimsCompatibilityReader(_Engine(failing="station_code = :store"), _Engine())  # type: ignore[arg-type]

    with pytest.raises(AimsUnavailable) as error:
        reader.fetch_labels("084")

    assert error.value.relation == "end_device"
    assert "hunter2" not in str(error.value)


def test_a_driver_error_while_reading_core_devices_is_classified() -> None:
    reader = AimsCompatibilityReader(_Engine(), _Engine(failing="ANY(:codes)"))  # type: ignore[arg-type]

    with pytest.raises(AimsUnavailable) as error:
        reader.fetch_labels("084")

    assert error.value.relation == "enddevice"
    assert "hunter2" not in str(error.value)
