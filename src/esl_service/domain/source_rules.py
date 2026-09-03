"""The procedure's source predicates and field mappings as named domain rules (#103).

``RefreshESL_New`` applied these in SQL, where an excluded row simply
vanished. The adapters (#91 to #94) now return rows raw, and each predicate
lives here once, named, pure, and tested, so an exclusion can be recorded
with its reason (FR-003, FR-006). Everything below is VERIFIED from the
decoded procedure text; nothing decides what the reference leaves open (no
campaign winner, no non-CLR UOM conversion, no display-page policy).

Rule ids cite ``docs/SPECIFICATION.md``: BR-002 item eligibility and product
mapping, BR-003 stock aggregation, BR-005 campaign inputs, BR-006 regular
price, BR-012 PFS, BR-013 UOM, BR-017 weekday metadata, BR-018 key isolation.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from esl_service.domain.canonical import ProductState
from esl_service.domain.promotion_evidence import (
    REASON_PRICE_AMBIGUOUS,
    REASON_PRICE_MISSING,
    PromotionType,
    WeekdayEvidence,
)

Row = Mapping[str, object]

#: The procedure's constants (VERIFIED).
REGULAR_PRICE_CATEGORY = "001"
MAIN_LOCATION_CODE = "001"
PRODUCT_URL_PREFIX = "https://threecellar.pepitosupermarket.com/product-detail/"
DEFAULT_CAMPAIGN_UOM = "PCS"
DISC_TEXT_LENGTH = 100

#: Issue codes for source-level exclusions and anomalies (#103).
ISSUE_ITEM_INACTIVE = "ITEM_INACTIVE"
ISSUE_SELLING_UOM_MISSING = "SELLING_UOM_MISSING"
ISSUE_REGULAR_PRICE_MISSING = REASON_PRICE_MISSING
ISSUE_REGULAR_PRICE_AMBIGUOUS = REASON_PRICE_AMBIGUOUS
ISSUE_BARCODE_AMBIGUOUS = "SELLING_BARCODE_AMBIGUOUS"
ISSUE_EXPIRY_DAYS_UNPARSEABLE = "EXPIRY_DAYS_UNPARSEABLE"
ISSUE_CAMPAIGN_TYPE_UNSUPPORTED = "CAMPAIGN_TYPE_UNSUPPORTED"
ISSUE_CAMPAIGN_HEADER_INACTIVE = "CAMPAIGN_HEADER_INACTIVE"
ISSUE_CAMPAIGN_DETAIL_CLOSED = "CAMPAIGN_DETAIL_CLOSED"

#: FactCampaign weekday columns, Monday first (Python ``weekday()`` order).
WEEKDAY_COLUMNS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
_ACTIVE_TOKENS = frozenset({"YES", "Y", "1", "TRUE"})
_RUNNING = "RUNNING"

#: The description-to-group ladder, in the procedure's order; first match wins.
_CAMPAIGN_GROUP_LADDER: tuple[tuple[tuple[str, ...], str], ...] = (
    (("SUPER", "SAVER"), "SUPER SAVER"),
    (("MODIS",), "MODIS"),
    (("QSR",), "QSR"),
    (("PFS",), "PFS"),
    (("RTE",), "RTE"),
    (("CLR",), "CLEARANCE"),
    (("JAPAS",), "JAJAN PASAR"),
    (("JAJAN", "PASAR"), "JAJAN PASAR"),
    (("SUPER", "FRESH"), "SUPER FRESH DEAL"),
    (("IN", "STORE", "PROMO"), "IN STORE PROMO"),
    (("PROMO", "IN", "STORE"), "IN STORE PROMO"),
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal):
        return value == 1
    return _text(value) == "1"


# --- items and mappings (BR-002) --------------------------------------------


def is_active_item(row: Row) -> bool:
    """``ISNULL(IM.ITM_STATUS, '') = 'O'``."""

    return _text(row.get("ITM_STATUS")) == "O"


def is_active_item_mapping(row: Row) -> bool:
    """``ISNULL(OID_ITM_STATUS, '') = 'O'`` on ``DimItemMapping``."""

    return _text(row.get("OID_ITM_STATUS")) == "O"


def is_selling_barcode_mapping(row: Row) -> bool:
    """``IUM_UOM_MAP_STATUS = 'O' AND IUM_MAIN_ITM_BARCODE = 1 AND IUM_SALES_UOM_FLAG = 1``."""

    return (
        _text(row.get("IUM_UOM_MAP_STATUS")) == "O"
        and _flag(row.get("IUM_MAIN_ITM_BARCODE"))
        and _flag(row.get("IUM_SALES_UOM_FLAG"))
    )


# --- price and stock (BR-003, BR-006) -----------------------------------------


def is_regular_price_row(row: Row) -> bool:
    """``BSP_PRICE_CATG = '001' AND BSP_STATUS = 'A'``: the physical-store regular price."""

    return _text(row.get("BSP_PRICE_CATG")) == REGULAR_PRICE_CATEGORY and _text(row.get("BSP_STATUS")) == "A"


def is_main_location(row: Row, column: str) -> bool:
    """``<column> = '001'``: only the store's main location counts for stock."""

    return _text(row.get(column)) == MAIN_LOCATION_CODE


def is_pending_pos_movement(row: Row) -> bool:
    """``STOCK_UPDATED_FLAG IS NULL``: a POS movement not yet applied to stock."""

    return row.get("STOCK_UPDATED_FLAG") is None


def _decimal(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def stock_on_hand(
    stock: Sequence[Row], offline: Sequence[Row], pos: Sequence[Row], item_code: str
) -> Decimal | None:
    """Sum stock the way the procedure does (BR-003), or None when the item has no rows.

    ``STOCK_MASTER``: current + consignment at the main location;
    ``OFFLINE_TEMP_ITEM_MOVEMENT``: quantity at the main location;
    ``POS_OFFLINE_TEMP_ITEM_MOVEMENT``: pending quantity at the main location.
    """

    total = Decimal(0)
    seen = False
    for row in stock:
        if _text(row.get("SM_ITM_CD")) == item_code and is_main_location(row, "SM_LOC_CD"):
            total += _decimal(row.get("SM_CURR_STK_QTY")) + _decimal(row.get("SM_CONSIGN_STK_QTY"))
            seen = True
    for row in offline:
        if _text(row.get("STR_ITM_CD")) == item_code and is_main_location(row, "LOC_CD"):
            total += _decimal(row.get("DBL_QTY"))
            seen = True
    for row in pos:
        if (
            _text(row.get("STR_ITM_CD")) == item_code
            and is_main_location(row, "LOC_CD")
            and is_pending_pos_movement(row)
        ):
            total += _decimal(row.get("DBL_QTY"))
            seen = True
    return total if seen else None


# --- campaigns (BR-005, BR-012, BR-014) ---------------------------------------


def is_active_campaign_header(row: Row) -> bool:
    """``CH.CMP_STATUS = 'A'``."""

    return _text(row.get("CMP_STATUS")) == "A"


def is_open_campaign_detail(row: Row) -> bool:
    """``CIGD.CIGD_STATUS = 'O'``."""

    return _text(row.get("CIGD_STATUS")) == "O"


def promotion_type_for(cmp_type: object) -> PromotionType | None:
    """``CMP_TYPE`` 0 percent, 1 fixed price, 3 value based; anything else (2, FREE ITEM) is unsupported."""

    try:
        code = int(str(cmp_type).strip()) if cmp_type is not None else None
    except ValueError:
        return None
    return {0: PromotionType.PERCENT, 1: PromotionType.FIXED_PRICE, 3: PromotionType.VALUE_BASED}.get(
        code if code is not None else -1
    )


def campaign_group_for(description: str | None) -> str | None:
    """The procedure's ``CASE WHEN CMP_DESC LIKE ...`` ladder; first rung wins."""

    if description is None:
        return None
    upper = description.upper()
    for tokens, group in _CAMPAIGN_GROUP_LADDER:
        position = 0
        matched = True
        for token in tokens:
            found = upper.find(token, position)
            if found < 0:
                matched = False
                break
            position = found + len(token)
        if matched:
            return group
    return None


def disc_text_for(description: str | None) -> str:
    """``RIGHT(ISNULL(CH.CMP_DESC, ''), 100)``, kept raw (BR-011)."""

    return (description or "")[-DISC_TEXT_LENGTH:]


def weekday_evidence_for(fact_row: Row | None, reference_date: date) -> WeekdayEvidence:
    """Weekday metadata from a RUNNING ``FactCampaign`` row, absent kept distinct (BR-017)."""

    if fact_row is None or _text(fact_row.get("CAMPAIGN STATUS")).upper() != _RUNNING:
        return WeekdayEvidence.MISSING
    value = _text(fact_row.get(WEEKDAY_COLUMNS[reference_date.weekday()])).upper()
    return WeekdayEvidence.ACTIVE if value in _ACTIVE_TOKENS else WeekdayEvidence.INACTIVE


# --- product state (BR-002) -----------------------------------------------------


def _optional_text(value: object) -> str | None:
    text = _text(value)
    return text or None


def product_state_for(row: Row, *, barcode: str | None, class_rotation: str | None) -> ProductState:
    """Map ``ITEM_MST`` the way the procedure fills ``#tmpResult``.

    ``CONSIGMENT``: ``'N'`` when ``ITM_ATTR18_DESC = 'TRADING'`` else ``'C'``;
    ``RETURNABLE``: ``'N'`` when ``ITM_ATTR13 = '00002'`` else ``'Y'``.
    ``NFC_URL`` is a constant blank and ``REDLIST`` is dead code (PR #80),
    both kept as the owner directed.
    """

    item_code = _text(row.get("ITM_CD"))
    brand = _text(row.get("ITM_ATTR07_DESC")).replace("\r", "").replace("\n", "")
    return ProductState(
        barcode=barcode,
        item_name=_optional_text(row.get("ITM_LONG_NAME")),
        item_shortname=_optional_text(row.get("ITM_NAME")),
        product_url=f"{PRODUCT_URL_PREFIX}{item_code}" if item_code else None,
        nfc_url=None,
        division=_optional_text(row.get("ITM_ATTR02_DESC")),
        department=_optional_text(row.get("ITM_ATTR04_DESC")),
        item_class=_optional_text(row.get("ITM_ATTR05_DESC")),
        subclass=_optional_text(row.get("ITM_ATTR06_DESC")),
        brand=brand or None,
        class_rotation=class_rotation,
        consignment=_text(row.get("ITM_ATTR18_DESC")).upper() != "TRADING",
        returnable=_text(row.get("ITM_ATTR13")) != "00002",
        red_list=False,
    )


def expiry_days_for(row: Row) -> tuple[int | None, str | None]:
    """``ITM_ATTR14_DESC`` copied verbatim by the procedure; here parsed or reported."""

    raw = row.get("ITM_ATTR14_DESC")
    if raw is None or _text(raw) == "":
        return None, None
    try:
        return int(Decimal(_text(raw))), None
    except (InvalidOperation, ValueError):
        return None, ISSUE_EXPIRY_DAYS_UNPARSEABLE
