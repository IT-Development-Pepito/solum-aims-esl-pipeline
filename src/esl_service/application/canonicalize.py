"""Four raw source tiers -> canonical records and record issues (#103).

This is the join the boundary decision of PR #80 left open: the adapters
(#91 to #94) return rows raw, and the domain holds the rules (#10, #12, #36,
#37, and the #103 source rules). The step calls those rules in the order the
procedure applied them and records every exclusion as a ``RecordIssue``, so
no row disappears without a reason (FR-003, FR-006). It decides nothing the
reference leaves open: several eligible campaigns go through the #37
``compatibility-v1`` strategy and stay unresolved with their ambiguity code;
a non-CLR campaign UOM is unresolved, not converted; and the display page is
supplied by an injected policy, because the AIMS page semantics are still
UNKNOWN / NEEDS-DISCOVERY (#24).

The step imports no adapter, no SQL, and no settings; the architecture tests
and its own import test keep it that way. Output is deterministic: records
are ordered by canonical key and hashed with the canonical serializer, so two
runs over identical input compare byte for byte.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from esl_service.application.contracts import (
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadResult,
    WarehouseProvenance,
    WarehouseReadResult,
)
from esl_service.domain.canonical import (
    CanonicalEslRecord,
    CanonicalKey,
    DisplayDecision,
    ExpiryState,
    InventoryState,
    PriceBasis,
    PricingState,
    PromotionStateData,
    Provenance,
)
from esl_service.domain.outcomes import (
    EligibilityStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import (
    PromotionEvaluationEvidence,
    WeekdayEvidence,
)
from esl_service.domain.promotion_rules import (
    SCALABLE_UOM,
    CampaignCandidate,
    evaluate,
    evaluate_candidate,
    to_display_price,
)
from esl_service.domain.promotion_selection import select_compatibility_state
from esl_service.domain.serialization import canonical_hash
from esl_service.domain.source_rules import (
    DEFAULT_CAMPAIGN_UOM,
    ISSUE_BARCODE_AMBIGUOUS,
    ISSUE_CAMPAIGN_DETAIL_CLOSED,
    ISSUE_CAMPAIGN_HEADER_INACTIVE,
    ISSUE_CAMPAIGN_TYPE_UNSUPPORTED,
    ISSUE_ITEM_INACTIVE,
    ISSUE_REGULAR_PRICE_AMBIGUOUS,
    ISSUE_REGULAR_PRICE_MISSING,
    ISSUE_SELLING_UOM_MISSING,
    campaign_group_for,
    disc_text_for,
    expiry_days_for,
    is_active_campaign_header,
    is_active_item,
    is_active_item_mapping,
    is_open_campaign_detail,
    is_regular_price_row,
    is_selling_barcode_mapping,
    product_state_for,
    promotion_type_for,
    stock_on_hand,
    weekday_evidence_for,
)
from esl_service.domain.validation import (
    CLASSIFICATION_VALIDATION,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    assess_record,
    validate_batch,
)

CANONICAL_SCHEMA_VERSION = "canonical-v1"
CALCULATION_VERSION = "calculation-v1"
SOURCE_ADAPTER_NAME = "sql-server-tiers-v1"
DEFAULT_CURRENCY = "IDR"
#: Reason code on every record until a display-page policy is approved (#24).
DISPLAY_PAGE_POLICY_UNDEFINED = "DISPLAY_PAGE_POLICY_UNDEFINED"
CLASSIFICATION_SOURCE = "SOURCE"

#: The procedure's defaults when a campaign has no time bounds.
_DAY_START = time(0, 0, 0)
_DAY_END = time(23, 59, 59)

Row = Mapping[str, object]
DisplayPolicy = Callable[[CanonicalKey, PromotionStateData | None], DisplayDecision]


def undefined_display_policy(key: CanonicalKey, promotion: PromotionStateData | None) -> DisplayDecision:
    """No page policy is approved: page 0 with an explicit reason, never a guess (BR-007 pending)."""

    return DisplayDecision(current_page=None, desired_page=0, reason_code=DISPLAY_PAGE_POLICY_UNDEFINED)


# --- input and output ----------------------------------------------------------


@dataclass(frozen=True)
class StoreSourceBundle:
    """One store's raw reads from the four tiers, as the adapters returned them."""

    store: StoreDirectoryEntry
    warehouse: WarehouseReadResult
    store_rows: StoreReadResult
    uom_mappings: UomMappingReadResult


@dataclass(frozen=True)
class KeyedIssue:
    """A record issue attributed to a store item, before or after a key exists."""

    store_code: str
    item_code: str
    key: CanonicalKey | None
    evidence: RecordIssueEvidence


@dataclass(frozen=True)
class CanonicalizationCounts:
    """Balanced counts over one store's batch (feeds the #25 reconciliation)."""

    extracted: int
    rejected: int
    valid: int
    eligible: int
    ineligible: int
    unresolved: int


@dataclass(frozen=True)
class CanonicalizationResult:
    records: tuple[CanonicalEslRecord, ...]
    evaluations: tuple[PromotionEvaluationEvidence, ...]
    assessments: tuple[RecordProcessingEvidence, ...]
    issues: tuple[KeyedIssue, ...]
    counts: CanonicalizationCounts
    record_hashes: tuple[str, ...] = field(default=())


# --- the step -------------------------------------------------------------------


def canonicalize_store(
    bundle: StoreSourceBundle,
    *,
    reference_time: datetime,
    configuration_version: str,
    rule_version: str,
    calculation_version: str = CALCULATION_VERSION,
    currency: str = DEFAULT_CURRENCY,
    display_policy: DisplayPolicy = undefined_display_policy,
) -> CanonicalizationResult:
    """Turn one store's raw reads into canonical records, evidence, and issues.

    ``reference_time`` stands in for the procedure's ``GETDATE()``: the
    instant campaigns are judged against, in the store's timezone.
    """

    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")

    store_code = bundle.store.store_code
    rows = bundle.store_rows
    index = _SourceIndex(bundle, store_code)
    references = _source_references(bundle)
    watermark = rows.provenance.source_watermark.isoformat()

    built: list[_Built] = []
    issues: list[KeyedIssue] = []
    rejected_before_key = 0

    for item_row in sorted(rows.items, key=lambda r: _text(r.get("ITM_CD"))):
        item_code = _text(item_row.get("ITM_CD"))
        if not is_active_item(item_row):
            issues.append(_issue(store_code, item_code, None, "BR-002", ISSUE_ITEM_INACTIVE, SEVERITY_ERROR, {"ITM_STATUS": _text(item_row.get("ITM_STATUS")) or None}))
            rejected_before_key += 1
            continue
        selling_uom = _text(item_row.get("ITM_SALES_UOM"))
        if not selling_uom:
            issues.append(_issue(store_code, item_code, None, "BR-018", ISSUE_SELLING_UOM_MISSING, SEVERITY_ERROR, {}))
            rejected_before_key += 1
            continue

        key = CanonicalKey(store_code, item_code, selling_uom)
        regular_price, ambiguous, price_issue = _regular_price(index, item_code, selling_uom)
        if price_issue is not None:
            issues.append(_issue(store_code, item_code, key, "BR-006", price_issue, SEVERITY_ERROR, {"selling_uom": selling_uom}))

        barcode, barcode_issue = _barcode(index, item_code)
        if barcode_issue is not None:
            issues.append(_issue(store_code, item_code, key, "BR-002", barcode_issue, SEVERITY_WARNING, {}))

        expiry_days, expiry_issue = expiry_days_for(item_row)
        if expiry_issue is not None:
            issues.append(_issue(store_code, item_code, key, "BR-002", expiry_issue, SEVERITY_WARNING, {"ITM_ATTR14_DESC": _text(item_row.get("ITM_ATTR14_DESC"))}))

        mapping = index.active_mapping.get(item_code)
        evaluation = _evaluate_promotions(
            index, key, reference_time, regular_price, ambiguous, rule_version, calculation_version, issues
        )
        promotion_state = evaluation.resulting_state if evaluation is not None else None
        decision = display_policy(key, promotion_state)

        record = CanonicalEslRecord(
            key=key,
            schema_version=CANONICAL_SCHEMA_VERSION,
            product=product_state_for(
                item_row,
                barcode=barcode,
                class_rotation=_optional(mapping, "CLASS_ROTATION_DAILY") if mapping else None,
            ),
            pricing=_pricing(selling_uom, regular_price, currency, calculation_version),
            inventory=InventoryState(
                stock_on_hand=stock_on_hand(
                    index.stock_by_item.get(item_code, ()),
                    index.offline_by_item.get(item_code, ()),
                    index.pos_by_item.get(item_code, ()),
                    item_code,
                ),
                product_weight=None,
                minimum_quantity=_quantity(mapping, "OID_REORDER_POINT"),
                maximum_quantity=_quantity(mapping, "OID_MAX_STOCK"),
                display_quantity=_quantity(mapping, "OID_DISPLAY_QTY"),
            ),
            expiry=ExpiryState(early_expiry_date=None, expiry_days=expiry_days),
            promotion_state=promotion_state,
            display_decision=decision,
            provenance=Provenance(
                adapter=SOURCE_ADAPTER_NAME,
                source_watermark=watermark,
                source_updated_at=None,
                configuration_version=configuration_version,
                rule_version=rule_version,
                source_references=references,
            ),
        )
        built.append(_Built(record, evaluation))

    records = tuple(b.record for b in built)
    validations = validate_batch(records)
    assessments = tuple(
        assess_record(
            validation=validation,
            evaluation=b.evaluation,
            current_page=b.record.display_decision.current_page,
            desired_page=b.record.display_decision.desired_page,
        )
        for validation, b in zip(validations, built, strict=True)
    )
    for b, assessment in zip(built, assessments, strict=True):
        for evidence in assessment.issues:
            issues.append(KeyedIssue(store_code, b.record.key.item_code, b.record.key, evidence))

    counts = _counts(len(rows.items), rejected_before_key, assessments)
    return CanonicalizationResult(
        records=records,
        evaluations=tuple(b.evaluation for b in built if b.evaluation is not None),
        assessments=assessments,
        issues=tuple(issues),
        counts=counts,
        record_hashes=tuple(canonical_hash(record) for record in records),
    )


# --- helpers ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    record: CanonicalEslRecord
    evaluation: PromotionEvaluationEvidence | None


class _SourceIndex:
    """Raw rows indexed once per store so each item is a lookup, not a scan."""

    def __init__(self, bundle: StoreSourceBundle, store_code: str) -> None:
        rows = bundle.store_rows
        self.prices: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
        for row in rows.selling_prices:
            if is_regular_price_row(row):
                value = _decimal(row.get("BSP_SELL_PRICE"))
                if value is not None:
                    self.prices[(_text(row.get("BSP_ITEM_CD")), _text(row.get("BSP_UOM")))].append(value)

        self.barcodes: dict[str, list[str]] = defaultdict(list)
        for row in bundle.uom_mappings.mappings:
            if is_selling_barcode_mapping(row):
                self.barcodes[_text(row.get("IUM_ITM_CD"))].append(_text(row.get("IUM_BAR_ITM_CD")))

        # Stock rows indexed once per store. The first live run (store 084,
        # 15,444 items against 88,485 rows) spent six minutes scanning every
        # row per item; slicing here keeps the domain sum, `stock_on_hand`,
        # unchanged and makes the step linear.
        self.stock_by_item: dict[str, list[Row]] = defaultdict(list)
        for row in rows.stock:
            self.stock_by_item[_text(row.get("SM_ITM_CD"))].append(row)
        self.offline_by_item: dict[str, list[Row]] = defaultdict(list)
        for row in rows.offline_movements:
            self.offline_by_item[_text(row.get("STR_ITM_CD"))].append(row)
        self.pos_by_item: dict[str, list[Row]] = defaultdict(list)
        for row in rows.pos_offline_movements:
            self.pos_by_item[_text(row.get("STR_ITM_CD"))].append(row)

        self.active_mapping: dict[str, Row] = {}
        for row in bundle.warehouse.item_mappings:
            if _text(row.get("OID_ORG_CD")) == store_code and is_active_item_mapping(row):
                self.active_mapping.setdefault(_text(row.get("OID_ITM_CD")), row)

        self.headers: dict[str, Row] = {_text(r.get("CMP_GRP_CD")): r for r in rows.campaign_headers}
        self.group_headers: dict[str, list[Row]] = defaultdict(list)
        for row in rows.campaign_item_group_headers:
            self.group_headers[_text(row.get("CMP_GRP_CD"))].append(row)
        self.conditions: dict[str, list[Row]] = defaultdict(list)
        for row in rows.campaign_item_group_conditions:
            self.conditions[_text(row.get("CIH_GRP_CD"))].append(row)
        self.condition_masters: dict[str, Row] = {_text(r.get("CND_CD")): r for r in rows.campaign_condition_masters}
        self.details_by_item: dict[str, list[Row]] = defaultdict(list)
        for row in rows.campaign_item_group_details:
            self.details_by_item[_text(row.get("CIGD_ITEM_CD"))].append(row)
        self.group_by_cigc: dict[str, str] = {}
        for code, headers in self.group_headers.items():
            for header in headers:
                self.group_by_cigc[_text(header.get("CIGC_CD"))] = code

        self.fact_by_campaign_item: dict[tuple[str, str], Row] = {}
        for row in bundle.warehouse.campaigns:
            if _text(row.get("FOR_ORGANIZATION")) == store_code:
                pair = (_text(row.get("CAMPAIGN CODE")), _text(row.get("CAMPAIGN ITEM")))
                self.fact_by_campaign_item.setdefault(pair, row)


def _regular_price(index: _SourceIndex, item_code: str, selling_uom: str) -> tuple[Decimal | None, bool, str | None]:
    values = sorted(set(index.prices.get((item_code, selling_uom), ())))
    if not values:
        return None, False, ISSUE_REGULAR_PRICE_MISSING
    if len(values) > 1:
        return None, True, ISSUE_REGULAR_PRICE_AMBIGUOUS
    return values[0], False, None


def _barcode(index: _SourceIndex, item_code: str) -> tuple[str | None, str | None]:
    values = sorted({b for b in index.barcodes.get(item_code, ()) if b})
    if not values:
        return None, None
    if len(values) > 1:
        return None, ISSUE_BARCODE_AMBIGUOUS
    return values[0], None


def _pricing(selling_uom: str, regular_price: Decimal | None, currency: str, calculation_version: str) -> PricingState:
    scalable = selling_uom.strip().upper() == SCALABLE_UOM
    return PricingState(
        currency=currency,
        source_regular_price=regular_price,
        source_member_price=None,
        source_price_basis=PriceBasis.KG if scalable else PriceBasis.EACH,
        display_regular_price=to_display_price(selling_uom, regular_price) if regular_price is not None else None,
        display_price_basis=PriceBasis.HUNDRED_GRAMS if scalable else PriceBasis.EACH,
        calculation_version=calculation_version,
    )


def _evaluate_promotions(
    index: _SourceIndex,
    key: CanonicalKey,
    reference_time: datetime,
    regular_price: Decimal | None,
    ambiguous: bool,
    rule_version: str,
    calculation_version: str,
    issues: list[KeyedIssue],
) -> PromotionEvaluationEvidence | None:
    details = index.details_by_item.get(key.item_code, ())
    if not details:
        return None

    # The procedure's SELECT DISTINCT: identical detail rows (VERIFIED, 1,098
    # pairs in store 084) collapse into one candidate. Rows of one campaign
    # that differ by UOM stay apart, each identified by its UOM.
    unique: dict[tuple[str, str, str, str], Row] = {}
    for detail in sorted(details, key=lambda d: _text(d.get("CIGC_CD"))):
        cigc = _text(detail.get("CIGC_CD"))
        campaign_code = index.group_by_cigc.get(cigc)
        if campaign_code is None:
            continue
        signature = (campaign_code, cigc, _text(detail.get("CIGD_UOM_CD")) or DEFAULT_CAMPAIGN_UOM, _text(detail.get("CIGD_STATUS")))
        unique.setdefault(signature, detail)
    uoms_per_campaign: dict[str, set[str]] = defaultdict(set)
    for campaign_code, _, uom, _ in unique:
        uoms_per_campaign[campaign_code].add(uom)

    evidence = []
    for (campaign_code, _, campaign_uom, _), detail in unique.items():
        header = index.headers.get(campaign_code)
        if header is None:
            continue
        if not is_active_campaign_header(header):
            issues.append(_campaign_issue(key, campaign_code, ISSUE_CAMPAIGN_HEADER_INACTIVE, {"CMP_STATUS": _text(header.get("CMP_STATUS"))}))
            continue
        if not is_open_campaign_detail(detail):
            issues.append(_campaign_issue(key, campaign_code, ISSUE_CAMPAIGN_DETAIL_CLOSED, {"CIGD_STATUS": _text(detail.get("CIGD_STATUS"))}))
            continue
        condition_master = _condition_master(index, campaign_code)
        promotion_type = promotion_type_for(condition_master.get("CMP_TYPE") if condition_master else None)
        if promotion_type is None:
            issues.append(_campaign_issue(key, campaign_code, ISSUE_CAMPAIGN_TYPE_UNSUPPORTED, {"CMP_TYPE": _text(condition_master.get("CMP_TYPE")) if condition_master else None}))
            continue
        description = header.get("CMP_DESC")
        candidate = CampaignCandidate(
            campaign_id=campaign_code,
            campaign_group=campaign_group_for(description if isinstance(description, str) else None),
            promotion_type=promotion_type,
            structured_value=_decimal(condition_master.get("CMP_PRM_VAL") if condition_master else None) or Decimal(0),
            raw_disc_text=disc_text_for(description if isinstance(description, str) else None) or None,
            start_date=_date(header.get("CMP_FROM_DATE")),
            end_date=_date(header.get("CMP_TO_DATE")),
            start_time=_time(header.get("CMP_FROM_TIME"), _DAY_START),
            end_time=_time(header.get("CMP_TO_TIME"), _DAY_END),
            campaign_uom=campaign_uom,
            weekday=weekday_evidence_for(index.fact_by_campaign_item.get((campaign_code, key.item_code)), reference_time.date()),
        )
        evaluated = evaluate_candidate(
            key=key,
            candidate=candidate,
            now=reference_time,
            regular_price=regular_price,
            regular_price_ambiguous=ambiguous,
        )
        if len(uoms_per_campaign[campaign_code]) > 1:
            # Same campaign, another UOM: a distinct candidate, same source campaign.
            evaluated = replace(evaluated, candidate_id=f"{campaign_code}/{campaign_uom}")
        evidence.append(evaluated)

    evaluation = evaluate(key, rule_version, calculation_version, tuple(evidence))
    return select_compatibility_state(evaluation, existing_state=None)


def _condition_master(index: _SourceIndex, campaign_code: str) -> Row | None:
    for header in index.group_headers.get(campaign_code, ()):
        for condition in index.conditions.get(_text(header.get("CIH_GRP_CD")), ()):
            master = index.condition_masters.get(_text(condition.get("CND_CD")))
            if master is not None:
                return master
    return None


def _counts(extracted: int, rejected_before_key: int, assessments: Sequence[RecordProcessingEvidence]) -> CanonicalizationCounts:
    rejected = rejected_before_key + sum(a.validation_status is ValidationStatus.REJECTED for a in assessments)
    valid = [a for a in assessments if a.validation_status is ValidationStatus.VALID]
    return CanonicalizationCounts(
        extracted=extracted,
        rejected=rejected,
        valid=len(valid),
        eligible=sum(a.eligibility_status is EligibilityStatus.ELIGIBLE for a in valid),
        ineligible=sum(a.eligibility_status is EligibilityStatus.INELIGIBLE for a in valid),
        unresolved=sum(a.eligibility_status is EligibilityStatus.UNRESOLVED for a in valid),
    )


def _source_references(bundle: StoreSourceBundle) -> tuple[str, ...]:
    def ref(provenance: WarehouseProvenance) -> str:
        return f"{provenance.instance}/{provenance.database}"

    return (ref(bundle.store_rows.provenance), ref(bundle.warehouse.provenance), ref(bundle.uom_mappings.provenance))


def _issue(store_code: str, item_code: str, key: CanonicalKey | None, rule_id: str, code: str, severity: str, evidence: Mapping[str, object]) -> KeyedIssue:
    return KeyedIssue(
        store_code,
        item_code,
        key,
        RecordIssueEvidence(
            rule_id=rule_id,
            issue_code=code,
            severity=severity,
            classification=CLASSIFICATION_SOURCE if code not in (ISSUE_REGULAR_PRICE_MISSING, ISSUE_REGULAR_PRICE_AMBIGUOUS) else CLASSIFICATION_VALIDATION,
            evidence={k: (v if isinstance(v, str | int | bool) or v is None else str(v)) for k, v in evidence.items()},
        ),
    )


def _campaign_issue(key: CanonicalKey, campaign_code: str, code: str, evidence: Mapping[str, object]) -> KeyedIssue:
    return _issue(key.store_code, key.item_code, key, "BR-005", code, SEVERITY_WARNING, {"campaign_id": campaign_code, **evidence})


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional(row: Row | None, column: str) -> str | None:
    if row is None:
        return None
    text = _text(row.get(column))
    return text or None


def _decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _quantity(row: Row | None, column: str) -> Decimal | None:
    return _decimal(row.get(column)) if row is not None else None


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(_text(value)[:10])


def _time(value: object, default: time) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    text = _text(value)
    if not text:
        return default
    try:
        return time.fromisoformat(text[:8])
    except ValueError:
        return default


__all__ = [
    "CALCULATION_VERSION",
    "CANONICAL_SCHEMA_VERSION",
    "DISPLAY_PAGE_POLICY_UNDEFINED",
    "SOURCE_ADAPTER_NAME",
    "CanonicalizationCounts",
    "CanonicalizationResult",
    "KeyedIssue",
    "StoreSourceBundle",
    "WeekdayEvidence",
    "canonicalize_store",
    "undefined_display_policy",
]
