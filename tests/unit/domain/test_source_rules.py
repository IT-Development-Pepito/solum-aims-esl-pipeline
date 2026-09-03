"""The procedure's source predicates and field mappings as named domain rules (#103).

Every function here is one predicate or mapping ``RefreshESL_New`` applied
in SQL, VERIFIED from the decoded procedure text and moved into the domain
so an excluded row can be recorded with its reason (FR-003, FR-006). None of
them decides anything the reference leaves open: no campaign winner, no
non-CLR UOM conversion, no page policy.
"""

from datetime import date
from decimal import Decimal

import pytest

from esl_service.domain.promotion_evidence import PromotionType, WeekdayEvidence
from esl_service.domain.source_rules import (
    ISSUE_EXPIRY_DAYS_UNPARSEABLE,
    MAIN_LOCATION_CODE,
    REGULAR_PRICE_CATEGORY,
    campaign_group_for,
    disc_text_for,
    expiry_days_for,
    is_active_campaign_header,
    is_active_item,
    is_active_item_mapping,
    is_main_location,
    is_open_campaign_detail,
    is_pending_pos_movement,
    is_regular_price_row,
    is_selling_barcode_mapping,
    product_state_for,
    promotion_type_for,
    stock_on_hand,
    weekday_evidence_for,
)

# --- item and mapping predicates (BR-002) ----------------------------------------


@pytest.mark.parametrize(("status", "active"), [("O", True), ("C", False), (None, False), (" ", False)])
def test_only_status_o_items_are_active(status: str | None, active: bool) -> None:
    assert is_active_item({"ITM_CD": "SKU-1", "ITM_STATUS": status}) is active


@pytest.mark.parametrize(("status", "active"), [("O", True), ("X", False), (None, False)])
def test_only_status_o_item_mappings_are_active(status: str | None, active: bool) -> None:
    assert is_active_item_mapping({"OID_ITM_STATUS": status}) is active


@pytest.mark.parametrize(
    ("row", "selling"),
    [
        ({"IUM_UOM_MAP_STATUS": "O", "IUM_MAIN_ITM_BARCODE": 1, "IUM_SALES_UOM_FLAG": 1}, True),
        ({"IUM_UOM_MAP_STATUS": "C", "IUM_MAIN_ITM_BARCODE": 1, "IUM_SALES_UOM_FLAG": 1}, False),
        ({"IUM_UOM_MAP_STATUS": "O", "IUM_MAIN_ITM_BARCODE": 0, "IUM_SALES_UOM_FLAG": 1}, False),
        ({"IUM_UOM_MAP_STATUS": "O", "IUM_MAIN_ITM_BARCODE": 1, "IUM_SALES_UOM_FLAG": 0}, False),
        ({"IUM_UOM_MAP_STATUS": "O", "IUM_MAIN_ITM_BARCODE": True, "IUM_SALES_UOM_FLAG": "1"}, True),
    ],
)
def test_the_selling_barcode_mapping_needs_all_three_flags(row: dict[str, object], selling: bool) -> None:
    assert is_selling_barcode_mapping(row) is selling


# --- price and stock predicates (BR-003, BR-006) ---------------------------------


@pytest.mark.parametrize(
    ("row", "regular"),
    [
        ({"BSP_PRICE_CATG": "001", "BSP_STATUS": "A"}, True),
        ({"BSP_PRICE_CATG": "002", "BSP_STATUS": "A"}, False),
        ({"BSP_PRICE_CATG": "001", "BSP_STATUS": "X"}, False),
        ({"BSP_PRICE_CATG": None, "BSP_STATUS": "A"}, False),
    ],
)
def test_only_active_category_001_rows_are_the_regular_price(row: dict[str, object], regular: bool) -> None:
    assert REGULAR_PRICE_CATEGORY == "001"
    assert is_regular_price_row(row) is regular


def test_only_the_main_location_counts_for_stock() -> None:
    assert MAIN_LOCATION_CODE == "001"
    assert is_main_location({"SM_LOC_CD": "001"}, "SM_LOC_CD") is True
    assert is_main_location({"SM_LOC_CD": "002"}, "SM_LOC_CD") is False
    assert is_main_location({"LOC_CD": "001"}, "LOC_CD") is True


def test_only_pos_movements_not_yet_applied_to_stock_count() -> None:
    assert is_pending_pos_movement({"STOCK_UPDATED_FLAG": None}) is True
    assert is_pending_pos_movement({"STOCK_UPDATED_FLAG": "Y"}) is False
    assert is_pending_pos_movement({"STOCK_UPDATED_FLAG": 1}) is False


def test_stock_on_hand_sums_the_three_sources_exactly_as_the_procedure_does() -> None:
    """current + consignment from STOCK_MASTER at 001, plus offline and pending POS movements at 001."""

    stock = (
        {"SM_ITM_CD": "SKU-1", "SM_LOC_CD": "001", "SM_CURR_STK_QTY": Decimal(10), "SM_CONSIGN_STK_QTY": Decimal(2)},
        {"SM_ITM_CD": "SKU-1", "SM_LOC_CD": "002", "SM_CURR_STK_QTY": Decimal(99), "SM_CONSIGN_STK_QTY": None},
        {"SM_ITM_CD": "SKU-2", "SM_LOC_CD": "001", "SM_CURR_STK_QTY": Decimal(5), "SM_CONSIGN_STK_QTY": None},
    )
    offline = ({"STR_ITM_CD": "SKU-1", "LOC_CD": "001", "DBL_QTY": Decimal("-1.5")},)
    pos = (
        {"STR_ITM_CD": "SKU-1", "LOC_CD": "001", "DBL_QTY": Decimal(-2), "STOCK_UPDATED_FLAG": None},
        {"STR_ITM_CD": "SKU-1", "LOC_CD": "001", "DBL_QTY": Decimal(-50), "STOCK_UPDATED_FLAG": "Y"},
    )

    assert stock_on_hand(stock, offline, pos, "SKU-1") == Decimal("8.5")
    assert stock_on_hand(stock, offline, pos, "SKU-2") == Decimal(5)
    assert stock_on_hand(stock, offline, pos, "SKU-9") is None


# --- campaign predicates and mappings (BR-005, BR-012, BR-014) ---------------------


def test_campaign_header_and_detail_status_predicates() -> None:
    assert is_active_campaign_header({"CMP_STATUS": "A"}) is True
    assert is_active_campaign_header({"CMP_STATUS": "I"}) is False
    assert is_open_campaign_detail({"CIGD_STATUS": "O"}) is True
    assert is_open_campaign_detail({"CIGD_STATUS": "C"}) is False


@pytest.mark.parametrize(
    ("cmp_type", "expected"),
    [(0, PromotionType.PERCENT), (1, PromotionType.FIXED_PRICE), (3, PromotionType.VALUE_BASED), (2, None), (None, None), ("0", PromotionType.PERCENT)],
)
def test_campaign_type_maps_to_a_structured_promotion_type_or_nothing(cmp_type: object, expected: PromotionType | None) -> None:
    """Type 2 (FREE ITEM) is read by the procedure but excluded from selection (CMP_TYPE IN (0,1,3))."""

    assert promotion_type_for(cmp_type) is expected


@pytest.mark.parametrize(
    ("description", "group"),
    [
        ("SUPER SAVER WEEKEND", "SUPER SAVER"),
        ("MODIS 10%", "MODIS"),
        ("QSR LUNCH", "QSR"),
        ("PFS MEMBER DEAL", "PFS"),
        ("RTE COMBO", "RTE"),
        ("CLR STOCK", "CLEARANCE"),
        ("JAPAS PAGI", "JAJAN PASAR"),
        ("JAJAN PASAR SORE", "JAJAN PASAR"),
        ("SUPER FRESH DEAL", "SUPER FRESH DEAL"),
        ("IN STORE PROMO A", "IN STORE PROMO"),
        ("PROMO IN STORE B", "IN STORE PROMO"),
        ("DISC 50%|ALL ITEM", None),
        (None, None),
    ],
)
def test_campaign_group_follows_the_procedures_description_ladder(description: str | None, group: str | None) -> None:
    assert campaign_group_for(description) == group


def test_the_first_matching_rung_of_the_ladder_wins() -> None:
    """SUPER SAVER is tested before PFS in the procedure, so a description with both is SUPER SAVER."""

    assert campaign_group_for("SUPER SAVER PFS") == "SUPER SAVER"


def test_disc_text_is_the_last_hundred_characters_of_the_description_kept_raw() -> None:
    assert disc_text_for("DISC 50%|ALL ITEM") == "DISC 50%|ALL ITEM"
    assert disc_text_for("x" * 150) == "x" * 100
    assert disc_text_for(None) == ""


# --- weekday evidence from FactCampaign (BR-017) -----------------------------------


def test_weekday_evidence_is_missing_without_a_running_fact_row() -> None:
    assert weekday_evidence_for(None, date(2026, 9, 2)) is WeekdayEvidence.MISSING
    assert weekday_evidence_for({"CAMPAIGN STATUS": "PLANNED", "WEDNESDAY": "YES"}, date(2026, 9, 2)) is WeekdayEvidence.MISSING


@pytest.mark.parametrize(("value", "evidence"), [("YES", WeekdayEvidence.ACTIVE), ("y", WeekdayEvidence.ACTIVE), ("1", WeekdayEvidence.ACTIVE), ("TRUE", WeekdayEvidence.ACTIVE), ("NO", WeekdayEvidence.INACTIVE), ("", WeekdayEvidence.INACTIVE), (None, WeekdayEvidence.INACTIVE)])
def test_weekday_evidence_reads_the_column_for_the_reference_date(value: str | None, evidence: WeekdayEvidence) -> None:
    wednesday = date(2026, 9, 2)

    assert weekday_evidence_for({"CAMPAIGN STATUS": "RUNNING", "WEDNESDAY": value}, wednesday) is evidence


# --- product state mapping (BR-002) ------------------------------------------------


def test_product_state_maps_the_item_master_columns_the_procedure_uses() -> None:
    row = {
        "ITM_CD": "SKU-1",
        "ITM_LONG_NAME": "Arabica Coffee 250g",
        "ITM_NAME": "Arabica",
        "ITM_ATTR02_DESC": "GROCERY",
        "ITM_ATTR04_DESC": "BEVERAGES",
        "ITM_ATTR05_DESC": "COFFEE",
        "ITM_ATTR06_DESC": "WHOLE BEAN",
        "ITM_ATTR07_DESC": "SOLUM\r\nBRAND",
        "ITM_ATTR18_DESC": "TRADING",
        "ITM_ATTR13": "00002",
    }

    product = product_state_for(row, barcode="899000000001", class_rotation="A")

    assert product.item_name == "Arabica Coffee 250g"
    assert product.item_shortname == "Arabica"
    assert product.division == "GROCERY"
    assert product.department == "BEVERAGES"
    assert product.item_class == "COFFEE"
    assert product.subclass == "WHOLE BEAN"
    assert product.brand == "SOLUMBRAND"
    assert product.product_url == "https://threecellar.pepitosupermarket.com/product-detail/SKU-1"
    assert product.consignment is False  # TRADING -> 'N'
    assert product.returnable is False  # ITM_ATTR13 == '00002' -> 'N'
    assert product.barcode == "899000000001"
    assert product.class_rotation == "A"
    assert product.nfc_url is None
    assert product.red_list is False  # REDLIST is dead code in the procedure; owner: keep as is


def test_product_flags_default_the_way_the_procedure_defaults_them() -> None:
    product = product_state_for({"ITM_CD": "SKU-2", "ITM_ATTR18_DESC": "OWN", "ITM_ATTR13": "00001"}, barcode=None, class_rotation=None)

    assert product.consignment is True  # anything but TRADING -> 'C'
    assert product.returnable is True
    assert product.barcode is None and product.item_name is None


# --- expiry days (a text attribute the procedure copies verbatim) --------------------


def test_expiry_days_parses_the_attribute_or_reports_it() -> None:
    assert expiry_days_for({"ITM_ATTR14_DESC": "14"}) == (14, None)
    assert expiry_days_for({"ITM_ATTR14_DESC": " 7 "}) == (7, None)
    assert expiry_days_for({"ITM_ATTR14_DESC": None}) == (None, None)
    assert expiry_days_for({"ITM_ATTR14_DESC": "two weeks"}) == (None, ISSUE_EXPIRY_DAYS_UNPARSEABLE)
