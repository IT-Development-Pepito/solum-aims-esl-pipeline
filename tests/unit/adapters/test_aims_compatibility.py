"""The read-only AIMS compatibility reader, without a database (#24, FR-020).

What this adapter decides is small and must be exact: which Core device
rows become labels, which are excluded and why, and what it records about
every read. Those decisions are pure functions here so they are testable
without either AIMS database. The join itself, the read-only session, and
the least-privilege proof are integration tests against the local clone.
"""

import inspect

import pytest

from esl_service.adapters.aims_compatibility import (
    AIMS_READ_ACTION,
    AIMS_READ_SCHEMA_VERSION,
    AimsCompatibilityReader,
    AimsSchemaDrift,
    CoreDevice,
    PortalLabel,
    ReadEvidence,
    merge_labels,
    parse_current_page,
)
from esl_service.application.contracts import AimsLabel, AimsReadModelReader
from esl_service.domain.failures import DependencyKind, FailureKind

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
