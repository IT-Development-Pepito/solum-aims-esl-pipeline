"""Complete, hand-checked fixtures for canonical domain contract tests."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from esl_service.domain import (
    CanonicalEslRecord,
    CanonicalKey,
    DisplayDecision,
    ExpiryState,
    InventoryState,
    PriceBasis,
    PricingState,
    ProductState,
    PromotionStateData,
    Provenance,
)
from esl_service.domain.outcomes import ExecutionMode, NewExecution, TriggerType


def canonical_record(
    *,
    store_code: str = "084",
    item_code: str = "101024011793",
    selling_uom: str = "KGS",
    source_regular_price: Decimal | None = Decimal(50000),
    display_regular_price: Decimal | None = Decimal(5000),
    source_price_basis: PriceBasis = PriceBasis.KG,
    display_price_basis: PriceBasis = PriceBasis.HUNDRED_GRAMS,
    promotion_state: PromotionStateData | None = None,
) -> CanonicalEslRecord:
    """Build the approved complete 084/101024011793/KGS/IDR canonical record."""
    return CanonicalEslRecord(
        key=CanonicalKey(store_code, item_code, selling_uom),
        schema_version="canonical-v1",
        product=ProductState(
            barcode="101024011793",
            item_name="Arabica Coffee",
            item_shortname="Arabica",
            product_url="https://products.example/101024011793",
            nfc_url="https://nfc.example/101024011793",
            division="GROCERY",
            department="BEVERAGES",
            item_class="COFFEE",
            subclass="WHOLE_BEAN",
            brand="SOLUM",
            class_rotation="A",
            consignment=False,
            returnable=False,
            red_list=False,
        ),
        pricing=PricingState(
            currency="IDR",
            source_regular_price=source_regular_price,
            source_member_price=Decimal(49000),
            source_price_basis=source_price_basis,
            display_regular_price=display_regular_price,
            display_price_basis=display_price_basis,
            calculation_version="rules-v1",
        ),
        inventory=InventoryState(
            stock_on_hand=Decimal("15.500"),
            product_weight=Decimal("1.000"),
            minimum_quantity=Decimal(2),
            maximum_quantity=Decimal(20),
            display_quantity=Decimal(8),
        ),
        expiry=ExpiryState(early_expiry_date=date(2026, 9, 15), expiry_days=14),
        promotion_state=promotion_state,
        display_decision=DisplayDecision(
            current_page=1, desired_page=2, reason_code="PRICE_CHANGED"
        ),
        provenance=Provenance(
            adapter="sql-server-source-v1",
            source_watermark="2026-08-28T10:00:00+08:00",
            source_updated_at=datetime(
                2026, 8, 28, 9, 59, tzinfo=timezone(timedelta(hours=8))
            ),
            configuration_version="config-v1",
            rule_version="rules-v1",
            source_references=("tb_ESL:084:101024011793",),
        ),
    )


def new_execution(
    configuration_version_id: UUID, **overrides: object
) -> NewExecution:
    """Build a complete execution request, overriding only what a test needs."""
    values: dict[str, object] = {
        "workflow_name": "sku-shadow",
        "store_code": "084",
        "trigger_type": TriggerType.SCHEDULED,
        "mode": ExecutionMode.SHADOW,
        "correlation_id": uuid4(),
        "source_window_start": datetime(2026, 8, 28, 7, 0, tzinfo=UTC),
        "source_window_end": datetime(2026, 8, 28, 7, 30, tzinfo=UTC),
        "configuration_version_id": configuration_version_id,
        "rule_version": "rules-v1",
        "requested_by": None,
        "reason": None,
    }
    values.update(overrides)
    return NewExecution(**values)  # type: ignore[arg-type]
