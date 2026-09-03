"""Persist one run's canonical output and reconcile it (#104).

The #103 step produces records, assessments, evaluations, and issues in
memory. This step writes them into the state store through the existing
repositories (#13 snapshots and differences, #12 outcomes, #36 evaluations,
#19 actions, #25 reconciliation), computes the diff against the store's
previous finalized snapshot by hash, creates INTENDED actions for changed
records, finalizes a balanced report, and, in shadow mode, records legacy
baseline mismatches as reconciliation exceptions that never claim parity.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.application.canonicalize import (
    CanonicalizationCounts,
    CanonicalizationResult,
    KeyedIssue,
)
from esl_service.application.contracts import (
    BaselineReadResult,
    SourceWindow,
    WarehouseProvenance,
)
from esl_service.application.persist_run import (
    CATEGORY_BASELINE_MISMATCH,
    CATEGORY_BASELINE_ROW_MISSING,
    DIFFERENCE_ADDED,
    DIFFERENCE_CHANGED,
    DIFFERENCE_REMOVED,
    EVENT_RECORD_EXCLUDED,
    REPRESENTATION_SOURCE_EXPECTED,
    ActiveModeUnsupported,
    PersistedRun,
    RunContext,
    persist_run,
)
from esl_service.domain.canonical import CanonicalEslRecord
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    ProcessingStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.reconciliation import ReconciliationMode
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.evidence_repository import (
    PromotionEvidenceRepository,
    RecordOutcomeRepository,
)
from esl_service.persistence.models import (
    ReconciliationReport,
    RecordAction,
    RecordDifference,
    RecordIssue,
    SnapshotSet,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

WINDOW = SourceWindow(datetime(2026, 9, 2, 0, 0, tzinfo=UTC), datetime(2026, 9, 2, 0, 30, tzinfo=UTC))
NOW = datetime(2026, 9, 2, 0, 30, 5, tzinfo=UTC)


# --- building a #103 result without the four tiers ------------------------------


def assessment(record: CanonicalEslRecord, *, issues: tuple[RecordIssueEvidence, ...] = ()) -> RecordProcessingEvidence:
    return RecordProcessingEvidence(
        key=record.key,
        validation_status=ValidationStatus.VALID,
        eligibility_status=EligibilityStatus.ELIGIBLE,
        promotion_outcome=None,
        current_page=None,
        desired_page=record.display_decision.desired_page,
        action_decision=ActionDecision.PAGE_CHANGE,
        processing_status=ProcessingStatus.ACTION_REQUIRED,
        issues=issues,
    )


def result_of(*records: CanonicalEslRecord, excluded: int = 0, keyless: tuple[KeyedIssue, ...] = ()) -> CanonicalizationResult:
    ordered = tuple(sorted(records, key=lambda r: (r.key.store_code, r.key.item_code, r.key.selling_uom)))
    return CanonicalizationResult(
        records=ordered,
        evaluations=(),
        assessments=tuple(assessment(r) for r in ordered),
        issues=keyless,
        counts=CanonicalizationCounts(
            extracted=len(ordered) + excluded,
            rejected=excluded,
            valid=len(ordered),
            eligible=len(ordered),
            ineligible=0,
            unresolved=0,
        ),
        record_hashes=(),
    )


def record(item_code: str, price: Decimal = Decimal(50000)) -> CanonicalEslRecord:
    return canonical_record(item_code=item_code, source_regular_price=price)


@pytest.fixture
def repositories(session: Session) -> dict[str, object]:
    return {
        "executions": ExecutionRepository(session),
        "snapshots": SnapshotRepository(session),
        "outcomes": RecordOutcomeRepository(session),
        "promotions": PromotionEvidenceRepository(session),
        "actions": ActionRepository(session),
        "reconciliation": ReconciliationRepository(session),
    }


LATER_WINDOW = SourceWindow(datetime(2026, 9, 2, 0, 30, tzinfo=UTC), datetime(2026, 9, 2, 1, 0, tzinfo=UTC))


def context(
    execution_id: UUID, mode: ExecutionMode = ExecutionMode.SHADOW, window: SourceWindow = WINDOW
) -> RunContext:
    return RunContext(
        execution_id=execution_id,
        store_code="084",
        mode=mode,
        configuration_hash="c" * 64,
        source_window=window,
        rule_version="rules-v1",
    )


def run(repositories: dict[str, object], execution_id: UUID, result: CanonicalizationResult, **overrides: object) -> PersistedRun:
    return persist_run(
        result,
        context(execution_id, overrides.pop("mode", ExecutionMode.SHADOW), overrides.pop("window", WINDOW)),  # type: ignore[arg-type]
        executions=repositories["executions"],  # type: ignore[arg-type]
        snapshots=repositories["snapshots"],  # type: ignore[arg-type]
        outcomes=repositories["outcomes"],  # type: ignore[arg-type]
        promotions=repositories["promotions"],  # type: ignore[arg-type]
        actions=repositories["actions"],  # type: ignore[arg-type]
        reconciliation=repositories["reconciliation"],  # type: ignore[arg-type]
        clock=lambda: NOW,
        **overrides,
    )


def new_run(session: Session, repositories: dict[str, object], configuration_version_id: UUID) -> UUID:
    executions: ExecutionRepository = repositories["executions"]  # type: ignore[assignment]
    execution = executions.create_execution(new_execution(configuration_version_id, store_code="084"))
    session.flush()
    return execution.id


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- the first shadow run ---------------------------------------------------------------


def test_a_first_shadow_run_persists_everything_and_a_balanced_report(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)

    persisted = run(repositories, execution_id, result_of(record("A"), record("B")))

    snapshot_set = session.get_one(SnapshotSet, persisted.snapshot_set_id)
    assert snapshot_set.representation_kind == REPRESENTATION_SOURCE_EXPECTED
    assert snapshot_set.record_count == 2 and snapshot_set.aggregate_hash is not None
    assert persisted.differences.added == 2 and persisted.differences.changed == 0
    assert persisted.differences.removed == 0 and persisted.differences.unchanged == 0
    assert count(session, RecordDifference) == 2
    assert {d.difference_type for d in session.scalars(select(RecordDifference))} == {DIFFERENCE_ADDED}
    assert count(session, RecordAction) == 2
    assert {a.state for a in session.scalars(select(RecordAction))} == {"INTENDED"}
    assert {a.mode for a in session.scalars(select(RecordAction))} == {"SHADOW"}
    report = session.get_one(ReconciliationReport, persisted.report_id)
    assert report.mode == ReconciliationMode.SHADOW.value
    assert (report.extracted, report.rejected, report.valid, report.eligible) == (2, 0, 2, 2)
    assert (report.intended, report.unchanged, report.unresolved) == (2, 0, 0)
    assert len(persisted.result_ids) == 2


def test_keyless_exclusions_become_events_and_count_as_rejected(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    """An inactive item has no canonical record, so no snapshot and no RecordIssue
    row can exist for it (the FK is NOT NULL); it is recorded as an execution
    event naming the rule and counted as rejected in the report."""

    execution_id = new_run(session, repositories, configuration_version_id)
    keyless = KeyedIssue(
        "084", "SKU-INACTIVE", None,
        RecordIssueEvidence(rule_id="BR-002", issue_code="ITEM_INACTIVE", severity="ERROR", classification="SOURCE", evidence={"ITM_STATUS": "C"}),
    )

    persisted = run(repositories, execution_id, result_of(record("A"), excluded=1, keyless=(keyless,)))

    executions: ExecutionRepository = repositories["executions"]  # type: ignore[assignment]
    events = [e for e in executions.list_events(execution_id) if e.event_type == EVENT_RECORD_EXCLUDED]
    assert len(events) == 1
    assert events[0].payload["item_code"] == "SKU-INACTIVE" and events[0].payload["issue_code"] == "ITEM_INACTIVE"
    report = session.get_one(ReconciliationReport, persisted.report_id)
    assert (report.extracted, report.rejected, report.valid) == (2, 1, 1)


def test_keyed_issues_are_persisted_against_the_record_outcome(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)
    rec = record("A")
    issue = RecordIssueEvidence(rule_id="BR-006", issue_code="CATEGORY_001_PRICE_MISSING", severity="ERROR", classification="VALIDATION", evidence={"selling_uom": "KGS"})
    result = CanonicalizationResult(
        records=(rec,),
        evaluations=(),
        assessments=(assessment(rec, issues=(issue,)),),
        issues=(KeyedIssue("084", "A", rec.key, issue),),
        counts=CanonicalizationCounts(1, 0, 1, 1, 0, 0),
    )

    persisted = run(repositories, execution_id, result)

    stored = session.scalars(select(RecordIssue)).all()
    assert [i.issue_code for i in stored] == ["CATEGORY_001_PRICE_MISSING"]
    reconciliation: ReconciliationRepository = repositories["reconciliation"]  # type: ignore[assignment]
    assert [e.category for e in reconciliation.list_exceptions(persisted.report_id)] == ["CATEGORY_001_PRICE_MISSING"]


# --- idempotency and the diff against the previous snapshot ----------------------------


def test_rerunning_the_same_execution_creates_no_duplicates(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)
    result = result_of(record("A"))

    first = run(repositories, execution_id, result)
    second = run(repositories, execution_id, result)

    assert second.snapshot_set_id == first.snapshot_set_id
    assert second.report_id == first.report_id
    assert count(session, SnapshotSet) == 1
    assert count(session, RecordAction) == 1
    assert count(session, ReconciliationReport) == 1
    assert second.resumed is True and first.resumed is False


def test_a_second_run_with_identical_input_has_zero_differences_and_no_actions(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    first_id = new_run(session, repositories, configuration_version_id)
    run(repositories, first_id, result_of(record("A"), record("B")))
    second_id = new_run(session, repositories, configuration_version_id)

    persisted = run(repositories, second_id, result_of(record("A"), record("B")))

    assert persisted.differences.unchanged == 2
    assert (persisted.differences.added, persisted.differences.changed, persisted.differences.removed) == (0, 0, 0)
    assert count(session, RecordDifference) == 2  # only the first run's ADDED rows
    assert count(session, RecordAction) == 2  # only the first run's actions
    report = session.get_one(ReconciliationReport, persisted.report_id)
    assert (report.unchanged, report.intended, report.eligible) == (2, 0, 2)


def test_a_changed_and_a_removed_record_are_recorded_by_hash(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    first_id = new_run(session, repositories, configuration_version_id)
    run(repositories, first_id, result_of(record("A"), record("B")))
    second_id = new_run(session, repositories, configuration_version_id)

    # A later run has its own source window; the #19 idempotency key includes
    # it, so the changed record gets a new logical action rather than the
    # first run's row (two runs over one window converge on one action).
    persisted = run(repositories, second_id, result_of(record("A", Decimal(52000))), window=LATER_WINDOW)

    assert (persisted.differences.changed, persisted.differences.removed, persisted.differences.unchanged) == (1, 1, 0)
    second_diffs = session.scalars(select(RecordDifference).where(RecordDifference.execution_id == second_id)).all()
    by_type = {d.difference_type: d for d in second_diffs}
    assert by_type[DIFFERENCE_CHANGED].changed_paths == ["pricing.source_regular_price"]
    assert by_type[DIFFERENCE_CHANGED].left_hash != by_type[DIFFERENCE_CHANGED].right_hash
    assert by_type[DIFFERENCE_REMOVED].right_snapshot_id is None
    actions = session.scalars(select(RecordAction).where(RecordAction.execution_id == second_id)).all()
    assert [a.item_code for a in actions] == ["A"]
    report = session.get_one(ReconciliationReport, persisted.report_id)
    assert (report.intended, report.unchanged, report.eligible) == (1, 0, 1)


def test_the_diff_baseline_is_the_stores_own_previous_snapshot(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    """Store 075's snapshot must not become store 084's baseline (BR-018)."""

    executions: ExecutionRepository = repositories["executions"]  # type: ignore[assignment]
    other = executions.create_execution(new_execution(configuration_version_id, store_code="075"))
    session.flush()
    other_record = canonical_record(store_code="075", item_code="A")
    other_context = RunContext(other.id, "075", ExecutionMode.SHADOW, "c" * 64, WINDOW, "rules-v1")
    persist_run(result_of(other_record), other_context, executions=executions, snapshots=repositories["snapshots"], outcomes=repositories["outcomes"], promotions=repositories["promotions"], actions=repositories["actions"], reconciliation=repositories["reconciliation"], clock=lambda: NOW)  # type: ignore[arg-type]
    mine = new_run(session, repositories, configuration_version_id)

    persisted = run(repositories, mine, result_of(record("A")))

    assert persisted.differences.added == 1 and persisted.differences.unchanged == 0


# --- mode and baseline -------------------------------------------------------------------


def test_active_mode_is_refused_until_the_aims_adapter_exists(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)

    with pytest.raises(ActiveModeUnsupported):
        run(repositories, execution_id, result_of(record("A")), mode=ExecutionMode.ACTIVE)

    assert count(session, SnapshotSet) == 0


def baseline(*rows: dict[str, object]) -> BaselineReadResult:
    return BaselineReadResult(
        rows=tuple(rows),
        provenance=WarehouseProvenance("sql.internal", "ESL", ("dbo.tb_ESL",), "tb-esl-baseline-v1", WINDOW.start, WINDOW.end, NOW),
    )


def legacy_row(item_code: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "STORE_CODE": "084", "ITEM_CODE": item_code, "UOM": "/100GR", "SALES_PRICE": 50000,
        "PROMO_FLAG": "0", "PROMOTION_TYPE": "", "DISC_TEXT": "", "BARCODE": "101024011793", "SOH": 15.5,
    }
    row.update(overrides)
    return row


def test_shadow_baseline_mismatches_become_exceptions_that_never_claim_parity(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)
    legacy = baseline(legacy_row("A", SALES_PRICE=49000), legacy_row("ZZZ-ONLY-IN-LEGACY"))

    persisted = run(repositories, execution_id, result_of(record("A"), record("B")), legacy_baseline=legacy)

    reconciliation: ReconciliationRepository = repositories["reconciliation"]  # type: ignore[assignment]
    exceptions = reconciliation.list_exceptions(persisted.report_id)
    by_category = {}
    for e in exceptions:
        by_category.setdefault(e.category, []).append(e)
    (mismatch,) = by_category[CATEGORY_BASELINE_MISMATCH]
    assert (mismatch.store_code, mismatch.item_code, mismatch.selling_uom) == ("084", "A", "KGS")
    assert mismatch.expected_evidence == {"source_regular_price": "50000"}
    assert mismatch.actual_evidence == {"SALES_PRICE": "49000"}
    (missing,) = by_category[CATEGORY_BASELINE_ROW_MISSING]
    assert missing.item_code == "B"
    assert "parity" not in " ".join(e.category for e in exceptions).lower()
    assert persisted.baseline.compared == 2 and persisted.baseline.mismatched == 1
    assert persisted.baseline.missing_in_legacy == 1 and persisted.baseline.only_in_legacy == 1


def test_a_matching_baseline_row_records_nothing(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)

    persisted = run(repositories, execution_id, result_of(record("A")), legacy_baseline=baseline(legacy_row("A")))

    reconciliation: ReconciliationRepository = repositories["reconciliation"]  # type: ignore[assignment]
    assert reconciliation.list_exceptions(persisted.report_id) == []
    assert persisted.baseline is not None and persisted.baseline.mismatched == 0


# --- checkpoints -------------------------------------------------------------------------


def test_a_step_id_gets_a_checkpoint_per_phase(
    session: Session, repositories: dict[str, object], configuration_version_id: UUID
) -> None:
    execution_id = new_run(session, repositories, configuration_version_id)
    executions: ExecutionRepository = repositories["executions"]  # type: ignore[assignment]
    step = executions.start_step(execution_id, "persist")

    persisted = run(repositories, execution_id, result_of(record("A")), step_id=step.id)

    from esl_service.persistence.models import ExecutionCheckpoint

    keys = [c.checkpoint_key for c in session.scalars(select(ExecutionCheckpoint).where(ExecutionCheckpoint.step_id == step.id).order_by(ExecutionCheckpoint.occurred_at))]
    assert keys == ["persist:snapshot", "persist:actions", "persist:report"]
    assert persisted.report_id is not None
