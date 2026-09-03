"""Persist one run's canonical output and reconcile it (#104).

The #103 step leaves records, assessments, evaluations, and issues in
memory. This step makes them durable through the repositories that already
exist and adds nothing new to the model:

1. one ``SOURCE_EXPECTED`` snapshot set for the execution (#13), finalized
   with its aggregate hash;
2. the store's differences against its own previous finalized snapshot,
   decided by canonical hash and recorded path by path (#13, BR-018);
3. one processing result with its issues per record (#12) and the promotion
   evaluation behind it (#36);
4. one ``INTENDED`` action per changed, eligible record (#19); in shadow mode
   nothing is submitted and the counts end in ``intended``;
5. a balanced reconciliation report (#25) whose exceptions come from the
   persisted evidence;
6. in shadow mode, the legacy ``tb_ESL`` baseline (#94) compared record by
   record, each difference an exception with the computed and the legacy
   values side by side. The categories say *mismatch*; nothing here claims
   parity, which stays gated by #38.

Idempotency rests on durable state, not on memory: the execution's own
finalized snapshot set is the proof the step already ran, so a restart
returns the existing identifiers instead of writing twice. A keyless
exclusion (an inactive item has no canonical record, and a record issue
must reference a snapshot) is recorded as an execution event and counted
as rejected; that limit is recorded in PROGRESS.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from uuid import UUID

from esl_service.application.canonicalize import CanonicalizationResult, KeyedIssue
from esl_service.application.contracts import BaselineReadResult, SourceWindow
from esl_service.domain.actions import NewRecordAction
from esl_service.domain.canonical import CanonicalEslRecord, CanonicalKey
from esl_service.domain.diff import FieldDifference, diff_payloads
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ExecutionMode,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionEvaluationEvidence
from esl_service.domain.promotion_selection import (
    REASON_DISPLAY_PRIORITY_SAME_ECONOMIC,
    REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC,
)
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.serialization import JSONValue

REPRESENTATION_SOURCE_EXPECTED = "SOURCE_EXPECTED"
DIFF_SCHEMA_VERSION = "diff-v1"
DIFFERENCE_ADDED = "ADDED"
DIFFERENCE_CHANGED = "CHANGED"
DIFFERENCE_REMOVED = "REMOVED"
DIFFERENCE_UNCHANGED = "UNCHANGED"
EVENT_RECORD_EXCLUDED = "RECORD_EXCLUDED"
ACTION_TYPE_PAGE_CHANGE = "PAGE_CHANGE"
CONTRACT_VERSION = "aims-page-v1"
CATEGORY_BASELINE_MISMATCH = "LEGACY_BASELINE_MISMATCH"
CATEGORY_BASELINE_ROW_MISSING = "LEGACY_BASELINE_ROW_MISSING"
CATEGORY_BASELINE_ROW_ONLY_IN_LEGACY = "LEGACY_BASELINE_ROW_ONLY_IN_LEGACY"
CHECKPOINT_SNAPSHOT = "persist:snapshot"
CHECKPOINT_ACTIONS = "persist:actions"
CHECKPOINT_REPORT = "persist:report"

_AMBIGUITY_CODES = frozenset(
    {REASON_PROMO_PRIORITY_DIFFERENT_ECONOMIC, REASON_DISPLAY_PRIORITY_SAME_ECONOMIC}
)
#: The procedure stores KGS items as ``/100GR`` in tb_ESL and maps it back for comparison.
_LEGACY_UOM_ALIASES = {"/100GR": "KGS"}


class ActiveModeUnsupported(RuntimeError):
    """Raised until the AIMS mutation adapter (#23) exists: only shadow runs persist."""


class PersistInterrupted(RuntimeError):
    """Raised when an unfinalized snapshot set exists; the runner must reconcile it."""


# --- ports: the repositories' shapes ---------------------------------------


class SnapshotRow(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def item_code(self) -> str: ...

    @property
    def selling_uom(self) -> str: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object]: ...


class SnapshotSetRow(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def aggregate_hash(self) -> str | None: ...


class WithId(Protocol):
    @property
    def id(self) -> UUID: ...


class SnapshotPort(Protocol):
    def find_snapshot_set(self, execution_id: UUID, representation_kind: str) -> SnapshotSetRow | None: ...

    def create_snapshot_set(
        self,
        *,
        execution_id: UUID,
        representation_kind: str,
        adapter_name: str,
        source_watermark: str,
        canonical_schema_version: str,
    ) -> SnapshotSetRow: ...

    def append_record(self, snapshot_set_id: UUID, record: CanonicalEslRecord) -> SnapshotRow: ...

    def append_difference(
        self,
        *,
        execution_id: UUID,
        difference_type: str,
        differences: Sequence[FieldDifference],
        diff_schema_version: str,
        rule_version: str,
        left_snapshot_id: UUID | None = None,
        right_snapshot_id: UUID | None = None,
    ) -> object: ...

    def finalize_snapshot_set(self, snapshot_set_id: UUID) -> SnapshotSetRow: ...

    def previous_finalized_records(
        self, store_code: str, representation_kind: str, *, exclude_execution_id: UUID
    ) -> Sequence[SnapshotRow]: ...


class OutcomePort(Protocol):
    def record_processing_result(
        self, execution_id: UUID, snapshot_id: UUID, evidence: RecordProcessingEvidence
    ) -> WithId: ...


class PromotionPort(Protocol):
    def record_evaluation(self, snapshot_id: UUID, evidence: PromotionEvaluationEvidence) -> object: ...


class ActionPort(Protocol):
    def create_intended(self, request: NewRecordAction) -> WithId: ...


class ReconciliationPort(Protocol):
    def finalize_report(
        self, execution_id: UUID, mode: ReconciliationMode, counts: ReconciliationCounts
    ) -> WithId: ...

    def latest_report(self, execution_id: UUID) -> WithId | None: ...

    def append_exception(
        self,
        report_id: UUID,
        *,
        category: str,
        store_code: str | None,
        item_code: str | None,
        selling_uom: str | None,
        expected_evidence: Mapping[str, JSONValue] | None,
        actual_evidence: Mapping[str, JSONValue] | None,
        record_processing_result_id: UUID | None = None,
    ) -> object: ...


class ExecutionPort(Protocol):
    def append_event(self, execution_id: UUID, event_type: str, payload: Mapping[str, object]) -> object: ...

    def append_checkpoint(
        self,
        step_id: UUID,
        *,
        checkpoint_key: str,
        checkpoint_version: int,
        watermark: str,
        payload: Mapping[str, object],
        payload_schema_version: str = "checkpoint-v1",
        payload_hash: str | None = None,
    ) -> object: ...


# --- inputs and outputs -------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """What every persisted row of this run must reference."""

    execution_id: UUID
    store_code: str
    mode: ExecutionMode
    configuration_hash: str
    source_window: SourceWindow
    rule_version: str
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True)
class DiffCounts:
    added: int
    changed: int
    removed: int
    unchanged: int


@dataclass(frozen=True)
class BaselineComparison:
    """What the shadow comparison against tb_ESL observed; evidence, not a verdict."""

    compared: int
    mismatched: int
    missing_in_legacy: int
    only_in_legacy: int


@dataclass(frozen=True)
class PersistedRun:
    snapshot_set_id: UUID
    report_id: UUID
    result_ids: tuple[UUID, ...]
    action_ids: tuple[UUID, ...]
    differences: DiffCounts
    baseline: BaselineComparison | None
    resumed: bool


# --- the step -------------------------------------------------------------------


def persist_run(
    result: CanonicalizationResult,
    context: RunContext,
    *,
    executions: ExecutionPort,
    snapshots: SnapshotPort,
    outcomes: OutcomePort,
    promotions: PromotionPort,
    actions: ActionPort,
    reconciliation: ReconciliationPort,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    legacy_baseline: BaselineReadResult | None = None,
    step_id: UUID | None = None,
) -> PersistedRun:
    """Persist one store's canonical output and finalize its reconciliation."""

    if context.mode is not ExecutionMode.SHADOW:
        raise ActiveModeUnsupported(
            "only SHADOW runs can be persisted until the AIMS mutation adapter (#23) exists"
        )

    existing = snapshots.find_snapshot_set(context.execution_id, REPRESENTATION_SOURCE_EXPECTED)
    if existing is not None:
        if existing.aggregate_hash is None:
            raise PersistInterrupted(
                f"execution {context.execution_id} has an unfinalized snapshot set; "
                "it must be reconciled before the step runs again"
            )
        report = reconciliation.latest_report(context.execution_id)
        if report is None:
            raise PersistInterrupted(
                f"execution {context.execution_id} has a finalized snapshot but no report"
            )
        return PersistedRun(
            snapshot_set_id=existing.id,
            report_id=report.id,
            result_ids=(),
            action_ids=(),
            differences=DiffCounts(0, 0, 0, 0),
            baseline=None,
            resumed=True,
        )

    watermark = _watermark(result, clock)
    evaluations = {e.key: e for e in result.evaluations}
    assessments = {a.key: a for a in result.assessments}

    # 1. snapshot, results, evaluations
    snapshot_set = snapshots.create_snapshot_set(
        execution_id=context.execution_id,
        representation_kind=REPRESENTATION_SOURCE_EXPECTED,
        adapter_name=_adapter_name(result),
        source_watermark=watermark,
        canonical_schema_version=_schema_version(result),
    )
    rows: list[tuple[CanonicalEslRecord, SnapshotRow, RecordProcessingEvidence, UUID]] = []
    for record in result.records:
        snapshot = snapshots.append_record(snapshot_set.id, record)
        evaluation = evaluations.get(record.key)
        if evaluation is not None:
            promotions.record_evaluation(snapshot.id, evaluation)
        assessment = assessments[record.key]
        stored = outcomes.record_processing_result(context.execution_id, snapshot.id, assessment)
        rows.append((record, snapshot, assessment, stored.id))
    snapshots.finalize_snapshot_set(snapshot_set.id)

    for issue in result.issues:
        if issue.key is None:
            executions.append_event(context.execution_id, EVENT_RECORD_EXCLUDED, _excluded_payload(issue))

    _checkpoint(executions, step_id, CHECKPOINT_SNAPSHOT, watermark, {"snapshot_set_id": str(snapshot_set.id), "records": len(rows)})

    # 2. differences against the store's previous snapshot, by hash
    previous = {
        (row.item_code, row.selling_uom): row
        for row in snapshots.previous_finalized_records(
            context.store_code, REPRESENTATION_SOURCE_EXPECTED, exclude_execution_id=context.execution_id
        )
    }
    classification: dict[CanonicalKey, str] = {}
    counts = {DIFFERENCE_ADDED: 0, DIFFERENCE_CHANGED: 0, DIFFERENCE_REMOVED: 0, DIFFERENCE_UNCHANGED: 0}
    for record, snapshot, _, _ in rows:
        prior = previous.pop((record.key.item_code, record.key.selling_uom), None)
        if prior is None:
            kind = DIFFERENCE_ADDED
            snapshots.append_difference(
                execution_id=context.execution_id,
                difference_type=kind,
                differences=(),
                diff_schema_version=DIFF_SCHEMA_VERSION,
                rule_version=context.rule_version,
                right_snapshot_id=snapshot.id,
            )
        elif prior.canonical_hash == snapshot.canonical_hash:
            kind = DIFFERENCE_UNCHANGED
        else:
            kind = DIFFERENCE_CHANGED
            snapshots.append_difference(
                execution_id=context.execution_id,
                difference_type=kind,
                # Stored payloads are already canonical JSON (#13); diffing them
                # directly is what makes the comparison reproducible from
                # durable state without re-serialising anything (FR-027).
                differences=diff_payloads(_stored(prior.payload), _stored(snapshot.payload)),
                diff_schema_version=DIFF_SCHEMA_VERSION,
                rule_version=context.rule_version,
                left_snapshot_id=prior.id,
                right_snapshot_id=snapshot.id,
            )
        classification[record.key] = kind
        counts[kind] += 1
    for removed in previous.values():
        snapshots.append_difference(
            execution_id=context.execution_id,
            difference_type=DIFFERENCE_REMOVED,
            differences=(),
            diff_schema_version=DIFF_SCHEMA_VERSION,
            rule_version=context.rule_version,
            left_snapshot_id=removed.id,
        )
        counts[DIFFERENCE_REMOVED] += 1

    # 3. intended actions for changed, eligible records
    action_ids: list[UUID] = []
    unchanged = skipped = 0
    for record, _, assessment, result_id in rows:
        if not _is_eligible(assessment):
            continue
        if classification[record.key] == DIFFERENCE_UNCHANGED:
            unchanged += 1
        elif assessment.action_decision is ActionDecision.PAGE_CHANGE:
            created = actions.create_intended(_intended(record, assessment, result_id, context))
            action_ids.append(created.id)
        else:
            skipped += 1
    _checkpoint(executions, step_id, CHECKPOINT_ACTIONS, watermark, {"intended": len(action_ids)})

    # 4. the balanced report
    unresolved = sum(1 for _, _, a, _ in rows if _is_unresolved(a))
    ambiguous = sum(1 for e in result.evaluations if _is_ambiguous(e))
    report_counts = ReconciliationCounts(
        extracted=result.counts.extracted,
        rejected=result.counts.rejected,
        valid=result.counts.valid,
        ineligible=result.counts.ineligible,
        eligible=result.counts.eligible + result.counts.unresolved,
        unchanged=unchanged,
        skipped_idempotent=skipped,
        intended=len(action_ids),
        acknowledged=0,
        rejected_by_aims=0,
        failed=0,
        unresolved=unresolved,
        submitted=0,
        ambiguous=min(ambiguous, unresolved),
    )
    report = reconciliation.finalize_report(context.execution_id, ReconciliationMode.SHADOW, report_counts)

    # 5. shadow comparison against the legacy baseline
    baseline = None
    if legacy_baseline is not None:
        baseline = _compare_baseline(reconciliation, report.id, rows, legacy_baseline, context.store_code)

    _checkpoint(executions, step_id, CHECKPOINT_REPORT, watermark, {"report_id": str(report.id)})

    return PersistedRun(
        snapshot_set_id=snapshot_set.id,
        report_id=report.id,
        result_ids=tuple(result_id for _, _, _, result_id in rows),
        action_ids=tuple(action_ids),
        differences=DiffCounts(
            added=counts[DIFFERENCE_ADDED],
            changed=counts[DIFFERENCE_CHANGED],
            removed=counts[DIFFERENCE_REMOVED],
            unchanged=counts[DIFFERENCE_UNCHANGED],
        ),
        baseline=baseline,
        resumed=False,
    )


# --- helpers ----------------------------------------------------------------------


def _watermark(result: CanonicalizationResult, clock: Callable[[], datetime]) -> str:
    for record in result.records:
        if record.provenance.source_watermark:
            return record.provenance.source_watermark
    return clock().isoformat()


def _adapter_name(result: CanonicalizationResult) -> str:
    return result.records[0].provenance.adapter if result.records else "sql-server-tiers-v1"


def _schema_version(result: CanonicalizationResult) -> str:
    return result.records[0].schema_version if result.records else "canonical-v1"


def _excluded_payload(issue: KeyedIssue) -> dict[str, object]:
    return {
        "store_code": issue.store_code,
        "item_code": issue.item_code,
        "rule_id": issue.evidence.rule_id,
        "issue_code": issue.evidence.issue_code,
        "severity": issue.evidence.severity,
        "evidence": dict(issue.evidence.evidence),
    }


def _checkpoint(
    executions: ExecutionPort, step_id: UUID | None, key: str, watermark: str, payload: Mapping[str, object]
) -> None:
    if step_id is None:
        return
    executions.append_checkpoint(
        step_id, checkpoint_key=key, checkpoint_version=1, watermark=watermark, payload=payload
    )


def _stored(payload: Mapping[str, object]) -> JSONValue:
    """A JSONB payload reloaded from a snapshot row is already canonical."""

    return cast("JSONValue", dict(payload))


def _is_eligible(assessment: RecordProcessingEvidence) -> bool:
    return (
        assessment.validation_status is ValidationStatus.VALID
        and assessment.eligibility_status is EligibilityStatus.ELIGIBLE
    )


def _is_unresolved(assessment: RecordProcessingEvidence) -> bool:
    return (
        assessment.validation_status is ValidationStatus.VALID
        and assessment.eligibility_status is EligibilityStatus.UNRESOLVED
    )


def _is_ambiguous(evaluation: PromotionEvaluationEvidence) -> bool:
    return any(
        code in _AMBIGUITY_CODES for candidate in evaluation.candidates for code in candidate.reason_codes
    )


def _intended(
    record: CanonicalEslRecord, assessment: RecordProcessingEvidence, result_id: UUID, context: RunContext
) -> NewRecordAction:
    page = assessment.desired_page if assessment.desired_page is not None else record.display_decision.desired_page
    return NewRecordAction(
        execution_id=context.execution_id,
        record_processing_result_id=result_id,
        key=record.key,
        label_code=None,
        action_type=ACTION_TYPE_PAGE_CHANGE,
        desired_page=page,
        desired_state=f"PAGE_{page}",
        mode=context.mode,
        contract_version=context.contract_version,
        rule_version=context.rule_version,
        configuration_hash=context.configuration_hash,
        source_window_start=context.source_window.start,
        source_window_end=context.source_window.end,
    )


# --- the shadow baseline comparison -----------------------------------------------------


def _compare_baseline(
    reconciliation: ReconciliationPort,
    report_id: UUID,
    rows: Sequence[tuple[CanonicalEslRecord, SnapshotRow, RecordProcessingEvidence, UUID]],
    legacy: BaselineReadResult,
    store_code: str,
) -> BaselineComparison:
    legacy_rows: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in legacy.rows:
        if _text(row.get("STORE_CODE")) != store_code:
            continue
        uom = _text(row.get("UOM")).upper()
        legacy_rows[(_text(row.get("ITEM_CODE")), _LEGACY_UOM_ALIASES.get(uom, uom))] = row

    compared = mismatched = missing = 0
    for record, _, _, result_id in rows:
        key = record.key
        compared += 1
        legacy_row = legacy_rows.pop((key.item_code, key.selling_uom.upper()), None)
        if legacy_row is None:
            missing += 1
            reconciliation.append_exception(
                report_id,
                category=CATEGORY_BASELINE_ROW_MISSING,
                store_code=key.store_code,
                item_code=key.item_code,
                selling_uom=key.selling_uom,
                expected_evidence=_computed_state(record),
                actual_evidence=None,
                record_processing_result_id=result_id,
            )
            continue
        expected, actual = _mismatches(record, legacy_row)
        if expected:
            mismatched += 1
            reconciliation.append_exception(
                report_id,
                category=CATEGORY_BASELINE_MISMATCH,
                store_code=key.store_code,
                item_code=key.item_code,
                selling_uom=key.selling_uom,
                expected_evidence=expected,
                actual_evidence=actual,
                record_processing_result_id=result_id,
            )
    for (item_code, uom), _ in sorted(legacy_rows.items()):
        reconciliation.append_exception(
            report_id,
            category=CATEGORY_BASELINE_ROW_ONLY_IN_LEGACY,
            store_code=store_code,
            item_code=item_code,
            selling_uom=uom,
            expected_evidence=None,
            actual_evidence={"ITEM_CODE": item_code, "UOM": uom},
        )
    return BaselineComparison(
        compared=compared, mismatched=mismatched, missing_in_legacy=missing, only_in_legacy=len(legacy_rows)
    )


def _computed_state(record: CanonicalEslRecord) -> dict[str, JSONValue]:
    promotion = record.promotion_state
    return {
        "source_regular_price": _str(record.pricing.source_regular_price),
        "promotion": promotion is not None,
        "promotion_type": promotion.promotion_type if promotion else None,
        "raw_disc_text": promotion.raw_disc_text if promotion else None,
        "barcode": record.product.barcode,
        "stock_on_hand": _str(record.inventory.stock_on_hand),
    }


def _mismatches(
    record: CanonicalEslRecord, legacy: Mapping[str, object]
) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """Compare the fields both sides define; a differing pair lands in both mappings."""

    expected: dict[str, JSONValue] = {}
    actual: dict[str, JSONValue] = {}
    promotion = record.promotion_state

    if not _same_decimal(record.pricing.source_regular_price, legacy.get("SALES_PRICE")):
        expected["source_regular_price"] = _str(record.pricing.source_regular_price)
        actual["SALES_PRICE"] = _str(legacy.get("SALES_PRICE"))
    legacy_promo = _text(legacy.get("PROMO_FLAG")) in ("1", "True", "true")
    if legacy_promo != (promotion is not None):
        expected["promotion"] = promotion is not None
        actual["PROMO_FLAG"] = _text(legacy.get("PROMO_FLAG")) or None
    if promotion is not None and legacy_promo:
        if _text(legacy.get("PROMOTION_TYPE")).upper() != _legacy_type(promotion.promotion_type):
            expected["promotion_type"] = promotion.promotion_type
            actual["PROMOTION_TYPE"] = _text(legacy.get("PROMOTION_TYPE")) or None
        if _text(legacy.get("DISC_TEXT")) != (promotion.raw_disc_text or ""):
            expected["raw_disc_text"] = promotion.raw_disc_text
            actual["DISC_TEXT"] = _text(legacy.get("DISC_TEXT")) or None
    if record.product.barcode is not None and _text(legacy.get("BARCODE")) != record.product.barcode:
        expected["barcode"] = record.product.barcode
        actual["BARCODE"] = _text(legacy.get("BARCODE")) or None
    if record.inventory.stock_on_hand is not None and not _same_decimal(
        record.inventory.stock_on_hand, legacy.get("SOH")
    ):
        expected["stock_on_hand"] = _str(record.inventory.stock_on_hand)
        actual["SOH"] = _str(legacy.get("SOH"))
    return expected, actual


def _legacy_type(promotion_type: str) -> str:
    return {"PERCENT": "PERCENT BASED", "FIXED_PRICE": "FIXED PRICE", "VALUE_BASED": "VALUE BASED"}.get(
        promotion_type, promotion_type
    )


def _same_decimal(computed: Decimal | None, legacy: object) -> bool:
    if computed is None:
        return legacy is None
    if legacy is None:
        return False
    try:
        return Decimal(str(legacy)) == computed
    except InvalidOperation:
        return False


def _str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


__all__ = [
    "ACTION_TYPE_PAGE_CHANGE",
    "CATEGORY_BASELINE_MISMATCH",
    "CATEGORY_BASELINE_ROW_MISSING",
    "CATEGORY_BASELINE_ROW_ONLY_IN_LEGACY",
    "DIFFERENCE_ADDED",
    "DIFFERENCE_CHANGED",
    "DIFFERENCE_REMOVED",
    "DIFFERENCE_UNCHANGED",
    "EVENT_RECORD_EXCLUDED",
    "REPRESENTATION_SOURCE_EXPECTED",
    "ActiveModeUnsupported",
    "BaselineComparison",
    "DiffCounts",
    "PersistInterrupted",
    "PersistedRun",
    "RunContext",
    "persist_run",
]
