"""Four raw source tiers -> canonical records and record issues (#103).

The step joins the adapters' raw rows to the domain rules and nothing else:
it calls the #36/#37 promotion rules, the #12 validation, and the #103
source rules, records every exclusion as an issue, and produces a
deterministic, hashable batch. It imports no adapter, no SQL, and no
settings; a page policy is injected because the AIMS page semantics are
still UNKNOWN / NEEDS-DISCOVERY.
"""

import ast
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from esl_service.application import canonicalize as canonicalize_module
from esl_service.application.canonicalize import (
    CANONICAL_SCHEMA_VERSION,
    DISPLAY_PAGE_POLICY_UNDEFINED,
    SOURCE_ADAPTER_NAME,
    CanonicalizationResult,
    StoreSourceBundle,
    canonicalize_store,
)
from esl_service.application.contracts import (
    STORE_OBJECTS,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadResult,
    WarehouseProvenance,
    WarehouseReadResult,
)
from esl_service.domain.canonical import DisplayDecision, PriceBasis
from esl_service.domain.outcomes import ProcessingStatus, ValidationStatus
from esl_service.domain.promotion_evidence import (
    REASON_PRICE_MISSING,
    REASON_UOM_RULE_REQUIRED,
    PromotionOutcome,
)
from esl_service.domain.serialization import canonical_hash
from esl_service.domain.source_rules import (
    ISSUE_CAMPAIGN_TYPE_UNSUPPORTED,
    ISSUE_ITEM_INACTIVE,
    ISSUE_REGULAR_PRICE_AMBIGUOUS,
    ISSUE_REGULAR_PRICE_MISSING,
    ISSUE_SELLING_UOM_MISSING,
)

JAKARTA = ZoneInfo("Asia/Jakarta")
REFERENCE = datetime(2026, 9, 2, 10, 0, tzinfo=JAKARTA)  # a Wednesday
WINDOW = SourceWindow(datetime(2026, 9, 2, 2, 30, tzinfo=UTC), datetime(2026, 9, 2, 3, 0, tzinfo=UTC))
STORE = StoreDirectoryEntry("084", "10.0.0.84", "PEPITO_084")
WATERMARK = datetime(2026, 9, 2, 3, 0, 1, tzinfo=UTC)

Row = Mapping[str, object]


def provenance(instance: str, database: str, objects: tuple[str, ...]) -> WarehouseProvenance:
    return WarehouseProvenance(
        instance=instance,
        database=database,
        objects=objects,
        query_version="test-v1",
        source_window_start=WINDOW.start,
        source_window_end=WINDOW.end,
        source_watermark=WATERMARK,
    )


def item(code: str, uom: str | None = "PCS", status: str = "O", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ITM_CD": code,
        "ITM_STATUS": status,
        "ITM_SALES_UOM": uom,
        "ITM_LONG_NAME": f"Item {code}",
        "ITM_NAME": code,
        "ITM_ATTR02_DESC": "GROCERY",
        "ITM_ATTR04_DESC": "DEPT",
        "ITM_ATTR05_DESC": "CLASS",
        "ITM_ATTR06_DESC": "SUB",
        "ITM_ATTR07_DESC": "BRAND",
        "ITM_ATTR13": "00001",
        "ITM_ATTR14_DESC": "14",
        "ITM_ATTR18_DESC": "TRADING",
        "ITM_SCALABLE_FLAG": 1 if uom == "KGS" else 0,
        "LAST_UPDATED_DATE": datetime(2026, 9, 1, 8, 0),  # noqa: DTZ001 - source columns are naive
    }
    row.update(extra)
    return row


def price(code: str, uom: str, value: str, category: str = "001", status: str = "A") -> dict[str, object]:
    return {"BSP_ORG_CD": "084", "BSP_ITEM_CD": code, "BSP_UOM": uom, "BSP_SELL_PRICE": Decimal(value), "BSP_PRICE_CATG": category, "BSP_STATUS": status}


def campaign(
    code: str,
    item_code: str,
    *,
    cmp_type: int = 0,
    value: str = "10",
    description: str = "DISC 10%|ALL",
    uom: str = "CLR",
    status: str = "A",
    detail_status: str = "O",
    from_date: datetime = datetime(2026, 9, 1),  # noqa: DTZ001
    to_date: datetime = datetime(2026, 9, 30),  # noqa: DTZ001
) -> dict[str, tuple[Row, ...]]:
    grp = f"G-{code}"
    return {
        "dbo.CMP_HDR": ({"CMP_GRP_CD": code, "CMP_DESC": description, "CMP_STATUS": status, "CMP_FROM_DATE": from_date, "CMP_TO_DATE": to_date, "CMP_FROM_TIME": "07:00:00", "CMP_TO_TIME": "23:00:00"},),
        "dbo.CMP_ORG_DTL": ({"CMP_GRP_CD": code, "CMP_ORG_CD": "084"},),
        "dbo.CMP_ITEM_GRP_HDR": ({"CMP_GRP_CD": code, "CIH_GRP_CD": grp, "CIGC_CD": f"C-{code}"},),
        "dbo.CMP_ITEM_GRP_CND": ({"CIH_GRP_CD": grp, "CND_CD": f"CND-{code}"},),
        "dbo.CMP_CND_MST": ({"CND_CD": f"CND-{code}", "CMP_TYPE": cmp_type, "CMP_PRM_VAL": Decimal(value)},),
        "dbo.CMP_ITEM_GRP_DTL": ({"CIGC_CD": f"C-{code}", "CIGD_ITEM_CD": item_code, "CIGD_STATUS": detail_status, "CIGD_UOM_CD": uom},),
    }


def merge(*parts: Mapping[str, tuple[Row, ...]]) -> dict[str, tuple[Row, ...]]:
    merged: dict[str, tuple[Row, ...]] = {name: () for name in STORE_OBJECTS}
    for part in parts:
        for name, rows in part.items():
            merged[name] = merged[name] + tuple(rows)
    return merged


def bundle(
    *,
    items: tuple[Row, ...],
    prices: tuple[Row, ...] = (),
    campaigns: Mapping[str, tuple[Row, ...]] | None = None,
    stock: tuple[Row, ...] = (),
    mappings: tuple[Row, ...] = (),
    uom_rows: tuple[Row, ...] = (),
    fact_campaigns: tuple[Row, ...] = (),
) -> StoreSourceBundle:
    store_rows = merge(
        {"dbo.ITEM_MST": items, "dbo.ITEM_DESCRIPTION": (), "dbo.BASIC_SP_MST": prices, "dbo.STOCK_MASTER": stock},
        campaigns or {},
    )
    return StoreSourceBundle(
        store=STORE,
        warehouse=WarehouseReadResult(
            item_mappings=mappings,
            campaigns=fact_campaigns,
            provenance=provenance("sql.internal", "DBWH_8555", ("dbo.DimItemMapping", "dbo.FactCampaign")),
        ),
        store_rows=StoreReadResult.from_mapping(store_rows, provenance("10.0.0.84", "PEPITO_084", STORE_OBJECTS)),
        uom_mappings=UomMappingReadResult(mappings=uom_rows, provenance=provenance("192.168.85.18", "PEPITO_HO", ("dbo.ITEM_UOM_MAPPING_MST",))),
    )


def run(source: StoreSourceBundle, **overrides: object) -> CanonicalizationResult:
    values: dict[str, object] = {
        "reference_time": REFERENCE,
        "configuration_version": "config-v1",
        "rule_version": "compatibility-v1",
    }
    values.update(overrides)
    return canonicalize_store(source, **values)  # type: ignore[arg-type]


# --- the happy path -----------------------------------------------------------------


def test_an_active_item_with_a_regular_price_becomes_one_canonical_record() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"),),
            prices=(price("SKU-1", "PCS", "12500"),),
            stock=({"SM_ITM_CD": "SKU-1", "SM_LOC_CD": "001", "SM_CURR_STK_QTY": Decimal(10), "SM_CONSIGN_STK_QTY": Decimal(0)},),
            mappings=({"OID_ORG_CD": "084", "OID_ITM_CD": "SKU-1", "OID_ITM_STATUS": "O", "OID_REORDER_POINT": 2, "OID_MAX_STOCK": 20, "OID_DISPLAY_QTY": 8, "CLASS_ROTATION_DAILY": "A"},),
            uom_rows=({"IUM_ITM_CD": "SKU-1", "IUM_LEAST_UOM_CD": "PCS", "IUM_BAR_ITM_CD": "899000000001", "IUM_UOM_MAP_STATUS": "O", "IUM_MAIN_ITM_BARCODE": 1, "IUM_SALES_UOM_FLAG": 1},),
        )
    )

    (record,) = result.records
    assert record.key.store_code == "084" and record.key.item_code == "SKU-1" and record.key.selling_uom == "PCS"
    assert record.schema_version == CANONICAL_SCHEMA_VERSION
    assert record.pricing.source_regular_price == Decimal(12500)
    assert record.pricing.display_regular_price == Decimal(12500)
    assert record.pricing.source_price_basis is PriceBasis.EACH
    assert record.pricing.currency == "IDR"
    assert record.inventory.stock_on_hand == Decimal(10)
    assert record.inventory.minimum_quantity == Decimal(2)
    assert record.inventory.maximum_quantity == Decimal(20)
    assert record.inventory.display_quantity == Decimal(8)
    assert record.product.barcode == "899000000001"
    assert record.product.class_rotation == "A"
    assert record.expiry.expiry_days == 14
    assert record.promotion_state is None
    assert record.provenance.adapter == SOURCE_ADAPTER_NAME
    assert record.provenance.rule_version == "compatibility-v1"
    assert record.provenance.configuration_version == "config-v1"
    assert record.provenance.source_watermark == WATERMARK.isoformat()
    assert set(record.provenance.source_references) >= {"10.0.0.84/PEPITO_084", "sql.internal/DBWH_8555", "192.168.85.18/PEPITO_HO"}
    assert result.issues == ()
    (assessment,) = result.assessments
    assert assessment.validation_status is ValidationStatus.VALID
    assert assessment.promotion_outcome is None


def test_a_scalable_item_is_priced_per_kilogram_and_displayed_per_100_grams() -> None:
    result = run(bundle(items=(item("SKU-K", uom="KGS"),), prices=(price("SKU-K", "KGS", "50000"),)))

    (record,) = result.records
    assert record.pricing.source_regular_price == Decimal(50000)
    assert record.pricing.source_price_basis is PriceBasis.KG
    assert record.pricing.display_regular_price == Decimal(5000)
    assert record.pricing.display_price_basis is PriceBasis.HUNDRED_GRAMS


# --- exclusions are issues, never silent drops -----------------------------------------


def test_an_inactive_item_is_excluded_with_an_issue_naming_the_rule() -> None:
    result = run(bundle(items=(item("SKU-1"), item("SKU-2", status="C")), prices=(price("SKU-1", "PCS", "100"),)))

    assert [r.key.item_code for r in result.records] == ["SKU-1"]
    (issue,) = result.issues
    assert issue.item_code == "SKU-2"
    assert issue.evidence.issue_code == ISSUE_ITEM_INACTIVE
    assert issue.evidence.rule_id == "BR-002"
    assert result.counts.extracted == 2 and result.counts.rejected == 1


def test_an_item_without_a_selling_uom_cannot_be_keyed_and_is_an_issue() -> None:
    result = run(bundle(items=(item("SKU-X", uom=None),)))

    assert result.records == ()
    (issue,) = result.issues
    assert issue.evidence.issue_code == ISSUE_SELLING_UOM_MISSING
    assert issue.evidence.rule_id == "BR-018"


def test_a_missing_regular_price_keeps_the_record_and_records_the_anomaly() -> None:
    """BR-006: no alternate category fallback; the record carries None and an issue."""

    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "100", category="002"),)))

    (record,) = result.records
    assert record.pricing.source_regular_price is None
    (issue,) = result.issues
    assert issue.evidence.issue_code == ISSUE_REGULAR_PRICE_MISSING and issue.evidence.rule_id == "BR-006"


def test_two_different_active_category_001_prices_are_ambiguous() -> None:
    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "100"), price("SKU-1", "PCS", "120"))))

    (record,) = result.records
    assert record.pricing.source_regular_price is None
    assert [i.evidence.issue_code for i in result.issues] == [ISSUE_REGULAR_PRICE_AMBIGUOUS]


def test_two_identical_category_001_rows_are_not_ambiguous() -> None:
    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "100"), price("SKU-1", "PCS", "100"))))

    assert result.records[0].pricing.source_regular_price == Decimal(100) and result.issues == ()


# --- promotions go through #36 and #37, never a new rule --------------------------------


def test_one_eligible_percent_campaign_is_selected_atomically() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"),),
            prices=(price("SKU-1", "PCS", "10000"),),
            campaigns=campaign("CMP-A", "SKU-1", cmp_type=0, value="10"),
            fact_campaigns=({"FOR_ORGANIZATION": "084", "CAMPAIGN CODE": "CMP-A", "CAMPAIGN ITEM": "SKU-1", "CAMPAIGN STATUS": "RUNNING", "WEDNESDAY": "YES"},),
        )
    )

    (record,) = result.records
    (evaluation,) = result.evaluations
    assert evaluation.outcome is PromotionOutcome.SELECTED
    assert record.promotion_state is not None
    assert record.promotion_state.source_campaign_id == "CMP-A"
    assert record.promotion_state.effective_price == Decimal(9000)
    assert record.promotion_state.raw_disc_text == "DISC 10%|ALL"
    assert record.promotion_state.campaign_group is None  # description matches no ladder rung
    assert result.issues == ()


def test_two_eligible_campaigns_with_different_economics_stay_unresolved_with_the_ambiguity_code() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"),),
            prices=(price("SKU-1", "PCS", "10000"),),
            campaigns=merge(campaign("CMP-A", "SKU-1", value="10"), campaign("CMP-B", "SKU-1", value="20")),
        )
    )

    (evaluation,) = result.evaluations
    assert evaluation.outcome is PromotionOutcome.UNRESOLVED
    assert result.records[0].promotion_state is None
    codes = {i.evidence.issue_code for i in result.issues}
    assert "PROMO_PRIORITY_DIFFERENT_ECONOMIC" in codes
    (assessment,) = result.assessments
    assert assessment.processing_status is ProcessingStatus.UNRESOLVED


def test_a_campaign_uom_that_is_not_clr_or_the_selling_uom_is_unresolved_not_converted() -> None:
    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "10000"),), campaigns=campaign("CMP-A", "SKU-1", uom="CTN")))

    assert REASON_UOM_RULE_REQUIRED in {i.evidence.issue_code for i in result.issues}
    assert result.records[0].promotion_state is None


def test_a_campaign_without_a_regular_price_is_unresolved_with_the_price_reason() -> None:
    result = run(bundle(items=(item("SKU-1"),), campaigns=campaign("CMP-A", "SKU-1")))

    codes = {i.evidence.issue_code for i in result.issues}
    assert REASON_PRICE_MISSING in codes and ISSUE_REGULAR_PRICE_MISSING in codes


def test_inactive_headers_closed_details_and_unsupported_types_are_not_candidates() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"),),
            prices=(price("SKU-1", "PCS", "10000"),),
            campaigns=merge(
                campaign("CMP-I", "SKU-1", status="I"),
                campaign("CMP-C", "SKU-1", detail_status="C"),
                campaign("CMP-F", "SKU-1", cmp_type=2),
            ),
        )
    )

    (evaluation,) = result.evaluations
    assert evaluation.candidates == ()
    assert evaluation.outcome is PromotionOutcome.NO_PROMOTION
    codes = [i.evidence.issue_code for i in result.issues]
    assert codes.count(ISSUE_CAMPAIGN_TYPE_UNSUPPORTED) == 1
    assert "CAMPAIGN_HEADER_INACTIVE" in codes and "CAMPAIGN_DETAIL_CLOSED" in codes


def test_a_campaign_outside_its_window_is_ineligible_by_the_36_rule() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"),),
            prices=(price("SKU-1", "PCS", "10000"),),
            campaigns=campaign("CMP-A", "SKU-1", from_date=datetime(2026, 10, 1), to_date=datetime(2026, 10, 31)),  # noqa: DTZ001
        )
    )

    (evaluation,) = result.evaluations
    assert evaluation.outcome is PromotionOutcome.NO_PROMOTION
    assert "OUTSIDE_DATE_TIME_WINDOW" in {i.evidence.issue_code for i in result.issues}


def test_the_reference_time_is_the_procedure_getdate_in_the_store_timezone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(bundle(items=(item("SKU-1"),)), reference_time=datetime(2026, 9, 2, 10, 0))  # noqa: DTZ001


# --- display policy is injected because the page semantics are unknown -------------------


def test_the_default_display_policy_is_explicitly_undefined() -> None:
    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "100"),)))

    decision = result.records[0].display_decision
    assert decision.reason_code == DISPLAY_PAGE_POLICY_UNDEFINED
    assert decision.current_page is None and decision.desired_page == 0


def test_a_caller_may_supply_a_display_policy() -> None:
    result = run(
        bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "100"),)),
        display_policy=lambda key, promotion: DisplayDecision(current_page=None, desired_page=1, reason_code="TEST"),
    )

    assert result.records[0].display_decision.desired_page == 1


# --- determinism and boundaries ----------------------------------------------------------


def test_the_batch_is_ordered_by_key_and_hashes_identically_on_identical_input() -> None:
    source = bundle(items=(item("SKU-2"), item("SKU-1")), prices=(price("SKU-1", "PCS", "1"), price("SKU-2", "PCS", "2")))

    first, second = run(source), run(source)

    assert [r.key.item_code for r in first.records] == ["SKU-1", "SKU-2"]
    assert [canonical_hash(r) for r in first.records] == [canonical_hash(r) for r in second.records]
    assert first.record_hashes == tuple(canonical_hash(r) for r in first.records)


def test_a_duplicate_canonical_key_is_rejected_by_validation_not_merged() -> None:
    result = run(bundle(items=(item("SKU-1"), item("SKU-1")), prices=(price("SKU-1", "PCS", "1"),)))

    statuses = [a.validation_status for a in result.assessments]
    assert statuses == [ValidationStatus.VALID, ValidationStatus.REJECTED]
    assert "DUPLICATE_CANONICAL_KEY" in {i.evidence.issue_code for i in result.issues}


def test_the_step_imports_no_adapter_sql_or_settings() -> None:
    tree = ast.parse(Path(canonicalize_module.__file__).read_text(encoding="utf-8"))
    imported = {
        name
        for node in ast.walk(tree)
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
    }
    forbidden = ("esl_service.adapters", "esl_service.persistence", "esl_service.config", "sqlalchemy", "pyodbc")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_counts_balance_over_the_batch() -> None:
    result = run(
        bundle(
            items=(item("SKU-1"), item("SKU-2", status="C"), item("SKU-3")),
            prices=(price("SKU-1", "PCS", "1"),),
            campaigns=merge(campaign("A", "SKU-3", value="10"), campaign("B", "SKU-3", value="20")),
        )
    )

    counts = result.counts
    assert counts.extracted == 3
    assert counts.rejected == 1  # inactive item
    assert counts.valid == 2
    assert counts.unresolved == 1  # SKU-3: ambiguous campaigns
    assert counts.valid == counts.eligible + counts.ineligible + counts.unresolved


# --- defects found by the first live run (store 084, 2026-09-03) --------------------------


def test_duplicate_campaign_detail_rows_collapse_into_one_candidate() -> None:
    """VERIFIED: 1,098 of 17,991 campaign+item pairs in store 084 carry exact
    duplicate CMP_ITEM_GRP_DTL rows. The procedure's SELECT DISTINCT collapses
    them; the step raised "candidate identifiers must be unique" instead."""

    campaigns = campaign("CMP-A", "SKU-1", value="10")
    detail = campaigns["dbo.CMP_ITEM_GRP_DTL"][0]
    campaigns["dbo.CMP_ITEM_GRP_DTL"] = (detail, dict(detail), dict(detail))

    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "10000"),), campaigns=campaigns))

    (evaluation,) = result.evaluations
    assert [c.candidate_id for c in evaluation.candidates] == ["CMP-A"]
    assert evaluation.outcome is PromotionOutcome.SELECTED


def test_one_campaign_with_two_uoms_for_an_item_keeps_both_candidates_apart() -> None:
    campaigns = campaign("CMP-A", "SKU-1", value="10")
    detail = campaigns["dbo.CMP_ITEM_GRP_DTL"][0]
    campaigns["dbo.CMP_ITEM_GRP_DTL"] = (detail, {**detail, "CIGD_UOM_CD": "CTN"})

    result = run(bundle(items=(item("SKU-1"),), prices=(price("SKU-1", "PCS", "10000"),), campaigns=campaigns))

    (evaluation,) = result.evaluations
    assert sorted(c.candidate_id for c in evaluation.candidates) == ["CMP-A/CLR", "CMP-A/CTN"]
    assert {c.source_campaign_id for c in evaluation.candidates} == {"CMP-A"}


def test_a_store_sized_batch_canonicalizes_in_seconds_not_minutes() -> None:
    """The first live run spent six minutes in this step: stock was summed by
    scanning every stock and movement row once per item (15,444 items against
    88,485 rows). Indexing once per store must make this linear."""

    import time

    items = tuple(item(f"SKU-{n:05d}") for n in range(6000))
    prices = tuple(price(f"SKU-{n:05d}", "PCS", "1000") for n in range(6000))
    stock = tuple(
        {"SM_ITM_CD": f"SKU-{n % 6000:05d}", "SM_LOC_CD": "001", "SM_CURR_STK_QTY": Decimal(1), "SM_CONSIGN_STK_QTY": None}
        for n in range(12000)
    )
    pos = tuple(
        {"STR_ITM_CD": f"SKU-{n % 6000:05d}", "LOC_CD": "001", "DBL_QTY": Decimal(-1), "STOCK_UPDATED_FLAG": None}
        for n in range(48000)
    )
    store_rows = merge({"dbo.ITEM_MST": items, "dbo.BASIC_SP_MST": prices, "dbo.STOCK_MASTER": stock, "dbo.POS_OFFLINE_TEMP_ITEM_MOVEMENT": pos})
    source = StoreSourceBundle(
        store=STORE,
        warehouse=WarehouseReadResult((), (), provenance("sql.internal", "DBWH_8555", ("dbo.DimItemMapping", "dbo.FactCampaign"))),
        store_rows=StoreReadResult.from_mapping(store_rows, provenance("10.0.0.84", "PEPITO_084", STORE_OBJECTS)),
        uom_mappings=UomMappingReadResult((), provenance("192.168.85.18", "PEPITO_HO", ("dbo.ITEM_UOM_MAPPING_MST",))),
    )

    started = time.perf_counter()
    result = run(source)
    elapsed = time.perf_counter() - started

    assert len(result.records) == 6000
    assert result.records[0].inventory.stock_on_hand == Decimal(2) - Decimal(8)
    assert elapsed < 5.0, f"took {elapsed:.1f}s"
