"""FR-004/FR-005/BR-018 tests for immutable canonical ESL records."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from esl_service.domain import (
    CanonicalKey,
    DisplayDecision,
    PriceBasis,
    PromotionStateData,
    canonical_hash,
    canonical_payload,
)
from tests.factories import canonical_record


def test_fr_004_kgs_preserves_source_and_display_basis() -> None:
    """FR-004 retains KGS economic evidence separately from the 100GR display value."""
    record = canonical_record(
        source_regular_price=Decimal(50000),
        display_regular_price=Decimal(5000),
        source_price_basis=PriceBasis.KG,
        display_price_basis=PriceBasis.HUNDRED_GRAMS,
    )

    assert record.pricing.source_regular_price == Decimal(50000)
    assert record.pricing.display_regular_price == Decimal(5000)
    assert record.pricing.source_price_basis is PriceBasis.KG
    assert record.pricing.display_price_basis is PriceBasis.HUNDRED_GRAMS


def test_fr_004_serializes_decimals_dates_enums_and_hashes_deterministically() -> None:
    """FR-004 normalizes canonical JSON and produces a repeatable UTF-8 SHA-256 hash."""
    record = canonical_record()

    payload = canonical_payload(record)

    assert payload["pricing"]["source_regular_price"] == "50000"
    assert payload["inventory"]["stock_on_hand"] == "15.5"
    assert payload["expiry"]["early_expiry_date"] == "2026-09-15"
    assert payload["provenance"]["source_updated_at"] == "2026-08-28T01:59:00+00:00"
    assert payload["pricing"]["display_price_basis"] == "100GR"
    assert canonical_hash(record) == canonical_hash(replace(record, inventory=replace(record.inventory, stock_on_hand=Decimal("15.5000"))))
    expected_canonical_json = (
        '{"display_decision":{"current_page":1,"desired_page":2,"reason_code":"PRICE_CHANGED"},'
        '"expiry":{"early_expiry_date":"2026-09-15","expiry_days":14},'
        '"inventory":{"display_quantity":"8","maximum_quantity":"20","minimum_quantity":"2",'
        '"product_weight":"1","stock_on_hand":"15.5"},'
        '"key":{"item_code":"101024011793","selling_uom":"KGS","store_code":"084"},'
        '"pricing":{"calculation_version":"rules-v1","currency":"IDR","display_price_basis":"100GR",'
        '"display_regular_price":"5000","source_member_price":"49000","source_price_basis":"KG",'
        '"source_regular_price":"50000"},'
        '"product":{"barcode":"101024011793","brand":"SOLUM","class_rotation":"A",'
        '"consignment":false,"department":"BEVERAGES","division":"GROCERY","item_class":"COFFEE",'
        '"item_name":"Arabica Coffee","item_shortname":"Arabica","nfc_url":"https://nfc.example/101024011793",'
        '"product_url":"https://products.example/101024011793","red_list":false,"returnable":false,'
        '"subclass":"WHOLE_BEAN"},"promotion_state":null,'
        '"provenance":{"adapter":"sql-server-source-v1","configuration_version":"config-v1",'
        '"rule_version":"rules-v1","source_references":["tb_ESL:084:101024011793"],'
        '"source_updated_at":"2026-08-28T01:59:00+00:00","source_watermark":"2026-08-28T10:00:00+08:00"},'
        '"schema_version":"canonical-v1"}'
    )
    assert (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        == expected_canonical_json
    )
    assert canonical_hash(record) == "369f225d5f871f993c9140c74ea1c1920fc469bff24b52f2e2c7f0b300d36c85"


def test_fr_004_rejects_float_values_from_canonical_serialization() -> None:
    """FR-004 prevents binary floating-point values from entering canonical JSON."""
    with pytest.raises(TypeError, match="unsupported canonical value: float"):
        canonical_payload(1.0)


@pytest.mark.parametrize(
    ("part", "value"),
    [("store_code", ""), ("item_code", "  "), ("selling_uom", "")],
)
def test_br_018_rejects_blank_canonical_key_parts(part: str, value: str) -> None:
    """BR-018 requires all three store/item/selling-UOM key boundaries."""
    key = {"store_code": "084", "item_code": "101024011793", "selling_uom": "KGS"}
    key[part] = value

    with pytest.raises(ValueError, match=part):
        CanonicalKey(**key)


def test_fr_005_contract_is_immutable_and_rejects_negative_pages() -> None:
    """FR-005 keeps a pure immutable domain boundary without page-policy defaults."""
    record = canonical_record()

    with pytest.raises(FrozenInstanceError):
        record.schema_version = "canonical-v2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="current_page"):
        DisplayDecision(current_page=-1, desired_page=0, reason_code="INVALID")


def test_fr_004_encodes_iso_8601_temporal_values() -> None:
    """FR-004 serializes all supported temporal values with their ISO-8601 form."""
    assert canonical_payload(date(2026, 8, 28)) == "2026-08-28"
    assert canonical_payload(time(9, 30, 15)) == "09:30:15"
    assert canonical_payload(
        datetime(2026, 8, 28, 9, 30, 15, tzinfo=UTC)
    ) == "2026-08-28T09:30:15+00:00"

def test_fr_004_normalizes_aware_datetimes_to_utc_for_payloads_and_hashes() -> None:
    """FR-004 treats equivalent aware instants as one canonical UTC value."""
    offset_value = datetime(
        2026, 8, 28, 17, 30, 15, tzinfo=timezone(timedelta(hours=8))
    )
    utc_value = datetime(2026, 8, 28, 9, 30, 15, tzinfo=UTC)

    assert canonical_payload(offset_value) == "2026-08-28T09:30:15+00:00"
    assert canonical_payload(offset_value) == canonical_payload(utc_value)
    assert canonical_hash(offset_value) == canonical_hash(utc_value)


def test_fr_004_rejects_naive_datetimes_from_canonical_serialization() -> None:
    """FR-004 requires timestamp values to identify an unambiguous UTC instant."""
    with pytest.raises(ValueError, match="naive datetime"):
        canonical_payload(datetime(2026, 8, 28, 9, 30, 15))  # noqa: DTZ001

def test_fr_004_preserves_raw_promotion_display_text_unchanged() -> None:
    """FR-004 retains raw DISC_TEXT as display/audit evidence without parsing it."""
    raw_disc_text = "  Promo | Harga Äœ |  "
    record = canonical_record(
        promotion_state=PromotionStateData(
            source_campaign_id="CAMPAIGN-084-1",
            promotion_flag="Y",
            promotion_type="PERCENT",
            campaign_group="WEEKEND",
            structured_value=Decimal(10),
            effective_price=Decimal(45000),
            display_price=Decimal(4500),
            discount_percentage=Decimal(10),
            saving_amount=Decimal(5000),
            raw_disc_text=raw_disc_text,
            starts_at=None,
            ends_at=None,
        )
    )

    assert canonical_payload(record)["promotion_state"]["raw_disc_text"] == raw_disc_text
