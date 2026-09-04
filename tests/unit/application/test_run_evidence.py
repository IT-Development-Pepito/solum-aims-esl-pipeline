"""Per-run operator evidence contracts (#109; FR-012, FR-022, NFR-007-009)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from esl_service.application.run_evidence import (
    EvidenceWithheld,
    IssueEvidenceRow,
    IssueQuery,
    MetricRunRow,
    ReconciliationExceptionRow,
    ReconciliationReportRow,
    ReportQuery,
    RunEvidenceRows,
    RunEvidenceService,
)
from esl_service.domain.authorization import Operation, Principal, Role
from tests.support.evidence import FakeEvidencePort as EvidencePort
from tests.support.evidence import issue_counts

NOW = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)
EXECUTION_ID = uuid4()
REPORT_ID = uuid4()


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_key: str
    watermark: str
    payload: dict[str, object]


@dataclass(frozen=True)
class Step:
    step_name: str
    attempt: int
    outcome: str
    failure_class: str | None
    started_at: datetime
    ended_at: datetime | None
    checkpoints: tuple[Checkpoint, ...] = ()


@dataclass(frozen=True)
class Execution:
    id: UUID = EXECUTION_ID
    workflow_name: str = "esl-refresh"
    store_code: str = "084"
    status: str = "SUCCEEDED_WITH_EXCEPTIONS"
    terminal_reason: str | None = None
    retry_not_before: datetime | None = None
    source_window_start: datetime = NOW - timedelta(minutes=30)
    source_window_end: datetime = NOW


@dataclass
class Authorizer:
    calls: list[tuple[str, Operation, str]] = field(default_factory=list)

    def authorize(self, principal: Principal, operation: Operation, *, resource_key: str) -> None:
        self.calls.append((principal.identity, operation, resource_key))


OPERATOR = Principal("budi", frozenset({Role.OPERATOR}))


def issue(
    item_code: str,
    issue_code: str,
    *,
    rule_id: str = "BR-013",
    severity: str = "ERROR",
    selling_uom: str | None = "KGS",
    evidence: dict[str, object] | None = None,
    keyless: bool = False,
) -> IssueEvidenceRow:
    return IssueEvidenceRow(
        store_code="084",
        item_code=item_code,
        selling_uom=selling_uom,
        rule_id=rule_id,
        issue_code=issue_code,
        severity=severity,
        evidence=evidence or {"reason": issue_code.lower()},
        keyless=keyless,
    )


def service(port: EvidencePort) -> tuple[RunEvidenceService, Authorizer]:
    authorizer = Authorizer()
    port.known_executions.add(EXECUTION_ID)
    return RunEvidenceService(authorizer, port, clock=lambda: NOW, metrics_run_limit=20), authorizer


def test_issue_summary_groups_before_stable_drilldown_pagination() -> None:
    """Removing grouping or applying pagination first would undercount a code."""

    port = EvidencePort(
        issues=(
            issue("200", "UOM_RULE_REQUIRED"),
            issue("100", "UOM_RULE_REQUIRED"),
            issue(
                "050",
                "INACTIVE_ITEM",
                rule_id="BR-001",
                severity="INFO",
                selling_uom=None,
                keyless=True,
            ),
        )
    )
    evidence, authorizer = service(port)

    result = evidence.issues(OPERATOR, EXECUTION_ID, IssueQuery(limit=1, offset=1))

    assert [(g.issue_code, g.rule_id, g.severity, g.count) for g in result.groups] == [
        ("UOM_RULE_REQUIRED", "BR-013", "ERROR", 2),
        ("INACTIVE_ITEM", "BR-001", "INFO", 1),
    ]
    assert result.total == 3
    assert [(row.item_code, row.issue_code, row.keyless) for row in result.records] == [
        ("100", "UOM_RULE_REQUIRED", False)
    ]
    assert authorizer.calls == [("budi", Operation.STATUS, str(EXECUTION_ID))]


def test_issue_filters_are_shared_by_summary_and_drilldown() -> None:
    """Ignoring code/severity/item filters would make CLI and API counts disagree."""

    port = EvidencePort(
        issues=(
            issue("100", "UOM_RULE_REQUIRED"),
            issue("100", "MISSING_PRICE", rule_id="BR-005", severity="WARNING"),
            issue("200", "UOM_RULE_REQUIRED"),
        )
    )
    evidence, _ = service(port)

    result = evidence.issues(
        OPERATOR,
        EXECUTION_ID,
        IssueQuery(code="MISSING_PRICE", severity="warning", item="100"),
    )

    assert result.total == 1
    assert [(group.issue_code, group.count) for group in result.groups] == [("MISSING_PRICE", 1)]
    assert result.records[0].evidence == {"reason": "missing_price"}


def test_issue_evidence_fails_closed_on_a_secret_like_key() -> None:
    """A stored credential-shaped key must never be returned by an operator read."""

    port = EvidencePort(issues=(issue("100", "BAD", evidence={"api_token": "needle"}),))
    evidence, _ = service(port)

    with pytest.raises(EvidenceWithheld, match="forbidden evidence key"):
        evidence.issues(OPERATOR, EXECUTION_ID)


def test_latest_report_groups_filtered_exceptions_and_keeps_legacy_values_side_by_side() -> None:
    """Using a stale revision or dropping either legacy value breaks reconciliation diagnosis."""

    counts = {
        "extracted": 2,
        "rejected": 0,
        "valid": 2,
        "ineligible": 0,
        "eligible": 2,
        "unchanged": 1,
        "skipped_idempotent": 0,
        "intended": 0,
        "acknowledged": 0,
        "rejected_by_aims": 0,
        "failed": 0,
        "unresolved": 1,
        "submitted": 0,
        "ambiguous": 1,
    }
    legacy = ReconciliationExceptionRow(
        sequence=3,
        category="LEGACY_BASELINE_MISMATCH",
        store_code="084",
        item_code="100",
        selling_uom="KGS",
        expected_evidence={"computed_price": "2.50"},
        actual_evidence={"legacy_price": "2.40"},
        resolution_status="OPEN",
    )
    port = EvidencePort(
        report=ReconciliationReportRow(
            report_id=REPORT_ID,
            revision=2,
            mode="SHADOW",
            status="FINALIZED",
            generated_at=NOW,
            finalized_at=NOW,
            counts=counts,
        ),
        exceptions=(
            ReconciliationExceptionRow(
                sequence=2,
                category="UOM_RULE_REQUIRED",
                store_code="084",
                item_code="200",
                selling_uom="PCS",
                expected_evidence=None,
                actual_evidence={"source_uom": "BOX"},
                resolution_status="OPEN",
            ),
            legacy,
        ),
    )
    evidence, _ = service(port)

    result = evidence.report(
        OPERATOR,
        EXECUTION_ID,
        ReportQuery(category="legacy_baseline_mismatch", item="100"),
    )

    assert result.revision == 2
    assert result.counts == counts
    assert [(group.category, group.count) for group in result.groups] == [
        ("LEGACY_BASELINE_MISMATCH", 1)
    ]
    assert result.exceptions[0].expected_evidence == {"computed_price": "2.50"}
    assert result.exceptions[0].actual_evidence == {"legacy_price": "2.40"}


def test_run_detail_derives_duration_counts_and_the_four_field_recovery_report() -> None:
    """Raw checkpoint payloads or missing recovery fields would force direct database access."""

    execution = Execution()
    steps = (
        Step(
            "canonicalize",
            1,
            "SUCCEEDED",
            None,
            NOW,
            NOW + timedelta(seconds=2, milliseconds=500),
            (
                Checkpoint(
                    "canonicalize:done",
                    "wm-1",
                    {
                        "records": 2,
                        "extracted": 3,
                        "rejected": 1,
                        "unresolved": 1,
                        "issues": 2,
                        "database_url": "must-not-pass-through",
                    },
                ),
            ),
        ),
    )
    port = EvidencePort(run_rows={EXECUTION_ID: RunEvidenceRows(execution=execution, steps=steps, uncertain_actions=())})
    evidence, _ = service(port)

    result = evidence.run_detail(OPERATOR, EXECUTION_ID)

    assert result.execution is execution
    assert result.steps[0].duration_seconds == 2.5
    assert result.steps[0].checkpoint_counts == {
        "records": 2,
        "extracted": 3,
        "rejected": 1,
        "unresolved": 1,
        "issues": 2,
    }
    assert result.recovery.scope == "esl-refresh:084"
    assert result.recovery.checkpoint == "canonicalize:done @ wm-1"
    assert result.recovery.resume_from == "discover"
    assert result.recovery.external_uncertainty == ()
    assert "Review the reconciliation report" in result.recovery.next_operator_action


def test_metrics_aggregate_the_configured_run_window_without_execution_labels() -> None:
    """Unbounded or per-execution series would create high-cardinality Prometheus data."""

    steps = (
        Step("canonicalize", 1, "SUCCEEDED", None, NOW, NOW + timedelta(seconds=2)),
    )
    port = EvidencePort(
        metrics=(
            MetricRunRow(
                workflow_name="esl-refresh",
                store_code="084",
                issues=issue_counts((issue("100", "UOM_RULE_REQUIRED"),)),
                reconciliation_counts={"unresolved": 1},
                steps=steps,
            ),
            MetricRunRow(
                workflow_name="esl-refresh",
                store_code="084",
                issues=issue_counts((issue("200", "UOM_RULE_REQUIRED"), issue("300", "MISSING_PRICE"))),
                reconciliation_counts={"unresolved": 2},
                steps=steps,
            ),
        )
    )
    evidence, _ = service(port)

    result = evidence.metrics(OPERATOR)

    assert port.metric_limit == 20
    assert [(m.workflow_name, m.store_code, m.issue_code, m.count) for m in result.issues] == [
        ("esl-refresh", "084", "MISSING_PRICE", 1),
        ("esl-refresh", "084", "UOM_RULE_REQUIRED", 2),
    ]
    assert [(m.count_name, m.count) for m in result.reconciliation] == [("unresolved", 3)]
    assert [(m.step_name, m.total_seconds, m.sample_count) for m in result.step_durations] == [
        ("canonicalize", 4.0, 2)
    ]
    assert "execution" not in str(result).lower()


def test_an_unknown_execution_is_a_lookup_error_not_an_empty_success() -> None:
    """The #29 UI must tell a wrong id from a clean run."""

    evidence, _ = service(EvidencePort())

    with pytest.raises(LookupError, match="no execution with id"):
        evidence.issues(OPERATOR, uuid4())
    with pytest.raises(LookupError, match="no execution with id"):
        evidence.report(OPERATOR, uuid4())


def test_a_run_without_a_report_yet_is_named_as_such() -> None:
    evidence, _ = service(EvidencePort())

    with pytest.raises(LookupError, match="has no reconciliation report yet"):
        evidence.report(OPERATOR, EXECUTION_ID)


def test_an_offset_past_the_end_is_an_empty_page_with_the_true_total() -> None:
    evidence, _ = service(EvidencePort(issues=(issue("100", "A"), issue("200", "A"))))

    result = evidence.issues(OPERATOR, EXECUTION_ID, IssueQuery(limit=10, offset=5))

    assert result.records == () and result.total == 2 and result.offset == 5


def test_report_counts_tolerate_a_partial_mapping_from_the_port() -> None:
    """The port is a Protocol; a missing count is zero, not a KeyError."""

    port = EvidencePort(
        report=ReconciliationReportRow(REPORT_ID, 1, "SHADOW", "FINALIZED", NOW, NOW, {"extracted": 5})
    )
    evidence, _ = service(port)

    result = evidence.report(OPERATOR, EXECUTION_ID)

    assert result.counts["extracted"] == 5 and result.counts["ambiguous"] == 0


def test_exception_evidence_fails_closed_on_a_secret_like_key() -> None:
    port = EvidencePort(
        report=ReconciliationReportRow(REPORT_ID, 1, "SHADOW", "FINALIZED", NOW, NOW, {}),
        exceptions=(
            ReconciliationExceptionRow(1, "X", "084", "A", None, None, {"db_password": "needle"}, "OPEN"),
        ),
    )
    evidence, _ = service(port)

    with pytest.raises(EvidenceWithheld) as withheld:
        evidence.report(OPERATOR, EXECUTION_ID, ReportQuery(category="X"))
    assert "needle" not in str(withheld.value)
