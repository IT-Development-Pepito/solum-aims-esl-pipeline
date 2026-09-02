"""The compatibility reader against the local AIMS clone (#24, FR-020, AD-003).

These tests need the clone described in docs/development/aims-local-clone.md,
reached through ESL_TEST_AIMS_PORTAL_URL and ESL_TEST_AIMS_CORE_URL as the
SELECT-only esl_aims_reader role. They skip anywhere the clone is absent,
including CI, and they never point at anything but localhost.

Counts drift with every refresh of the clone, so assertions are structural:
which store a label belongs to, that no unassigned device leaks through as a
page, that a write is refused, and that every read leaves audit evidence.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from esl_service.adapters.aims_compatibility import (
    AIMS_READ_ACTION,
    AIMS_READ_RESOURCE,
    AimsCompatibilityReader,
    AimsSchemaDrift,
    AuditedReadSink,
    create_read_only_engine,
)
from esl_service.application.contracts import AimsReadModelReader
from esl_service.persistence.models import AuditEntry
from esl_service.persistence.reconciliation_repository import ReconciliationRepository


def _clone_url(name: str) -> str:
    raw = os.environ.get(name)
    if not raw:
        pytest.skip(f"{name} is required; see docs/development/aims-local-clone.md")
    url = make_url(raw)
    if url.host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(f"{name} must point at a local clone, never at AIMS itself")
    return raw


@pytest.fixture(scope="module")
def portal_engine() -> Iterator[Engine]:
    engine = create_read_only_engine(make_url(_clone_url("ESL_TEST_AIMS_PORTAL_URL")))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def core_engine() -> Iterator[Engine]:
    engine = create_read_only_engine(make_url(_clone_url("ESL_TEST_AIMS_CORE_URL")))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def reader(portal_engine: Engine, core_engine: Engine) -> AimsCompatibilityReader:
    return AimsCompatibilityReader(portal_engine, core_engine)


# --- the read model, as the port promises (#22) ----------------------------


def test_the_reader_is_the_port(reader: AimsCompatibilityReader) -> None:
    assert isinstance(reader, AimsReadModelReader)


def test_labels_for_the_fitted_store_belong_to_it_and_carry_a_real_page(
    reader: AimsCompatibilityReader,
) -> None:
    labels = reader.fetch_labels("084")

    assert labels, "store 084 is the fitted store and must return labels"
    assert {label.store_code for label in labels} == {"084"}
    assert all(label.page >= 0 for label in labels), "no unassigned device may leak through"
    assert len({label.label_code for label in labels}) == len(labels), "one row per label"


def test_the_unfitted_store_returns_almost_nothing(reader: AimsCompatibilityReader) -> None:
    """Store 075 is computed by SQL Server but never reaches AIMS (VERIFIED)."""

    assert len(reader.fetch_labels("075")) <= 2


def test_the_malformed_store_code_row_is_not_folded_into_a_store(
    reader: AimsCompatibilityReader,
) -> None:
    """A read-only adapter reports vendor data as it is; it does not repair it."""

    assert reader.fetch_labels('"075') == () or all(
        label.store_code == '"075' for label in reader.fetch_labels('"075')
    )


def test_an_unknown_store_returns_an_empty_sequence(reader: AimsCompatibilityReader) -> None:
    assert reader.fetch_labels("999") == ()


# --- least privilege, proven rather than reviewed (acceptance criterion 1) --


def test_the_session_refuses_a_write_even_before_permissions_are_consulted(
    portal_engine: Engine,
) -> None:
    """Defence in depth: the session is read-only regardless of the role's grants."""

    with portal_engine.connect() as connection, pytest.raises(DBAPIError) as error:
        connection.execute(text("DELETE FROM end_device_templates WHERE false"))

    assert "read-only" in str(error.value).lower()


def test_the_configured_identity_has_no_write_grant(portal_engine: Engine) -> None:
    """Even outside the read-only session, the role itself cannot modify AIMS."""

    with portal_engine.connect() as connection:
        granted = connection.execute(
            text(
                "SELECT bool_or(has_table_privilege(current_user, c.oid, 'INSERT')"
                "  OR has_table_privilege(current_user, c.oid, 'UPDATE')"
                "  OR has_table_privilege(current_user, c.oid, 'DELETE'))"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        ).scalar_one()

    assert granted is False


# --- schema validation (acceptance criterion 2) ------------------------------


def test_pointing_at_a_database_without_the_tables_is_schema_drift(
    portal_engine: Engine,
) -> None:
    """The state store has no enddevice table; that is drift, classified, not a trace."""

    state_url = os.environ.get("ESL_TEST_DATABASE_URL")
    if not state_url:
        pytest.skip("ESL_TEST_DATABASE_URL is required for this drift check")
    wrong_core = create_read_only_engine(make_url(state_url))
    try:
        with pytest.raises(AimsSchemaDrift) as error:
            AimsCompatibilityReader(portal_engine, wrong_core).fetch_labels("084")
    finally:
        wrong_core.dispose()

    assert "enddevice" in str(error.value)
    assert "://" not in str(error.value)


# --- every read is audit-visible (acceptance criterion 2) --------------------


def test_every_read_appends_an_audit_entry_with_counts_and_no_location(
    portal_engine: Engine, core_engine: Engine, session: Session
) -> None:
    sink = AuditedReadSink(ReconciliationRepository(session), actor="test-suite")
    reader = AimsCompatibilityReader(portal_engine, core_engine, sink=sink)

    labels = reader.fetch_labels("084")

    entry = session.scalars(
        select(AuditEntry).where(
            AuditEntry.action == AIMS_READ_ACTION,
            AuditEntry.resource_type == AIMS_READ_RESOURCE,
            AuditEntry.resource_key == "084",
        )
    ).one()
    assert entry.execution_id is None, "a compatibility read is not an execution"
    assert entry.after_evidence is not None
    assert entry.after_evidence["labels"] == len(labels)
    assert entry.after_evidence["store_code"] == "084"
    assert "unassigned" in entry.after_evidence
    assert "://" not in str(entry.after_evidence)
