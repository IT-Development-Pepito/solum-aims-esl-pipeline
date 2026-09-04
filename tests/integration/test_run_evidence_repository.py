"""Read persisted #104 evidence through the #109 repository contract."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.application.canonicalize import (
    CanonicalizationCounts,
    CanonicalizationResult,
    KeyedIssue,
)
from esl_service.application.persist_run import RunContext, persist_run
from esl_service.domain.outcomes import ExecutionMode, RecordIssueEvidence
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import (
    PromotionEvidenceRepository,
    RecordOutcomeRepository,
)
from esl_service.persistence.models import RecordIssue
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.run_evidence_repository import RunEvidenceRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import new_execution
from tests.integration.test_persist_run import (
    NOW,
    WINDOW,
    assessment,
    baseline,
    legacy_row,
    record,
)


def test_persisted_issue_report_and_keyless_event_are_read_back_without_sql_workarounds(
    session: Session, configuration_version_id: UUID
) -> None:
    """Dropping either issue source or selecting an old report revision breaks #109 parity."""

    executions = ExecutionRepository(session)
    reconciliation = ReconciliationRepository(session)
    execution = executions.create_execution(
        new_execution(configuration_version_id, store_code="084")
    )
    session.flush()
    canonical = record("A")
    keyed = RecordIssueEvidence(
        rule_id="BR-006",
        issue_code="CATEGORY_001_PRICE_MISSING",
        severity="ERROR",
        classification="VALIDATION",
        evidence={"selling_uom": "KGS"},
    )
    keyless = KeyedIssue(
        "084",
        "SKU-INACTIVE",
        None,
        RecordIssueEvidence(
            rule_id="BR-002",
            issue_code="ITEM_INACTIVE",
            severity="ERROR",
            classification="SOURCE",
            evidence={"ITM_STATUS": "C"},
        ),
    )
    result = CanonicalizationResult(
        records=(canonical,),
        evaluations=(),
        assessments=(assessment(canonical, issues=(keyed,)),),
        issues=(keyless,),
        counts=CanonicalizationCounts(2, 1, 1, 1, 0, 0),
    )
    persisted = persist_run(
        result,
        RunContext(
            execution.id,
            "084",
            ExecutionMode.SHADOW,
            "c" * 64,
            WINDOW,
            "rules-v1",
        ),
        executions=executions,
        snapshots=SnapshotRepository(session),
        outcomes=RecordOutcomeRepository(session),
        promotions=PromotionEvidenceRepository(session),
        actions=ActionRepository(session),
        reconciliation=reconciliation,
        legacy_baseline=baseline(legacy_row("A", SALES_PRICE=49000)),
        clock=lambda: NOW,
    )

    # A later reconciliation revision is the operator contract; revision 1
    # must never leak into the read after revision 2 exists.
    first = reconciliation.latest_report(execution.id)
    assert first is not None and first.id == persisted.report_id
    counts = ReconciliationCounts(
        extracted=first.extracted,
        rejected=first.rejected,
        valid=first.valid,
        ineligible=first.ineligible,
        eligible=first.eligible,
        unchanged=first.unchanged,
        skipped_idempotent=first.skipped_idempotent,
        intended=first.intended,
        acknowledged=first.acknowledged,
        rejected_by_aims=first.rejected_by_aims,
        failed=first.failed,
        unresolved=first.unresolved,
        submitted=first.submitted,
        ambiguous=first.ambiguous,
    )
    latest = reconciliation.finalize_report(
        execution.id, ReconciliationMode.SHADOW, counts
    )
    reconciliation.append_exception(
        latest.id,
        category="LEGACY_BASELINE_MISMATCH",
        store_code="084",
        item_code="A",
        selling_uom="KGS",
        expected_evidence={"source_regular_price": "50000"},
        actual_evidence={"SALES_PRICE": "49000"},
    )

    repository = RunEvidenceRepository(session)
    issues = repository.issues_for(execution.id)
    report = repository.latest_report_for(execution.id)

    direct_issue_count = session.scalar(select(func.count()).select_from(RecordIssue))
    assert direct_issue_count == 1
    assert [(row.issue_code, row.item_code, row.keyless) for row in issues] == [
        ("CATEGORY_001_PRICE_MISSING", "A", False),
        ("ITEM_INACTIVE", "SKU-INACTIVE", True),
    ]
    assert report is not None and report.revision == 2
    legacy = next(
        item for item in report.exceptions if item.category == "LEGACY_BASELINE_MISMATCH"
    )
    assert legacy.expected_evidence == {"source_regular_price": "50000"}
    assert legacy.actual_evidence == {"SALES_PRICE": "49000"}


def test_metric_query_keeps_the_latest_n_runs_of_each_workflow_store_scope(
    session: Session, configuration_version_id: UUID
) -> None:
    """A global or oldest-first limit would hide active stores or stale the trend."""

    executions = ExecutionRepository(session)
    for store in ("084", "075"):
        for index, code in enumerate(("OLD", "MID", "NEW")):
            execution = executions.create_execution(
                new_execution(
                    configuration_version_id,
                    workflow_name="esl-refresh",
                    store_code=store,
                ),
                now=NOW + timedelta(seconds=index),
            )
            executions.append_event(
                execution.id,
                "RECORD_EXCLUDED",
                {
                    "store_code": store,
                    "item_code": f"{store}-{code}",
                    "rule_id": "BR-002",
                    "issue_code": code,
                    "severity": "ERROR",
                    "evidence": {"status": "C"},
                },
            )

    rows = RunEvidenceRepository(session).metric_evidence(per_scope_limit=2)

    assert len(rows) == 4
    assert {(row.workflow_name, row.store_code) for row in rows} == {
        ("esl-refresh", "084"),
        ("esl-refresh", "075"),
    }
    codes = [issue.issue_code for row in rows for issue in row.issues]
    assert sorted(codes) == ["MID", "MID", "NEW", "NEW"]
