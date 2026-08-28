"""Immutable, persistence-free canonical ESL record contracts."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True)
class CanonicalKey:
    """The BR-018 business boundary for one store item and selling UOM."""

    store_code: str
    item_code: str
    selling_uom: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("store_code", self.store_code),
            ("item_code", self.item_code),
            ("selling_uom", self.selling_uom),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")


class PriceBasis(StrEnum):
    """The unit basis associated with a source or display price."""

    EACH = "EACH"
    KG = "KG"
    HUNDRED_GRAMS = "100GR"


@dataclass(frozen=True)
class ProductState:
    """Product, classification, and flag evidence in a canonical record."""

    barcode: str | None
    item_name: str | None
    item_shortname: str | None
    product_url: str | None
    nfc_url: str | None
    division: str | None
    department: str | None
    item_class: str | None
    subclass: str | None
    brand: str | None
    class_rotation: str | None
    consignment: bool | None
    returnable: bool | None
    red_list: bool | None


@dataclass(frozen=True)
class PricingState:
    """Source economic evidence and separately-derived display price evidence."""

    currency: str
    source_regular_price: Decimal | None
    source_member_price: Decimal | None
    source_price_basis: PriceBasis
    display_regular_price: Decimal | None
    display_price_basis: PriceBasis
    calculation_version: str


@dataclass(frozen=True)
class InventoryState:
    """Inventory and product-weight quantities, represented as Decimal values."""

    stock_on_hand: Decimal | None
    product_weight: Decimal | None
    minimum_quantity: Decimal | None
    maximum_quantity: Decimal | None
    display_quantity: Decimal | None


@dataclass(frozen=True)
class ExpiryState:
    """Validated expiry evidence from the source representation."""

    early_expiry_date: date | None
    expiry_days: int | None


@dataclass(frozen=True)
class PromotionStateData:
    """One complete promotion state; no promotion selection policy is encoded here."""

    source_campaign_id: str
    promotion_flag: str | None
    promotion_type: str
    campaign_group: str | None
    structured_value: Decimal | None
    effective_price: Decimal | None
    display_price: Decimal | None
    discount_percentage: Decimal | None
    saving_amount: Decimal | None
    raw_disc_text: str | None
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class DisplayDecision:
    """Observed and desired display-page state without label action behavior."""

    current_page: int | None
    desired_page: int
    reason_code: str

    def __post_init__(self) -> None:
        if self.current_page is not None and self.current_page < 0:
            raise ValueError("current_page must not be negative")
        if self.desired_page < 0:
            raise ValueError("desired_page must not be negative")


@dataclass(frozen=True)
class Provenance:
    """Versioned source and rule evidence required to reproduce a record."""

    adapter: str
    source_watermark: str | None
    source_updated_at: datetime | None
    configuration_version: str
    rule_version: str
    source_references: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalEslRecord:
    """The complete immutable canonical record used for FR-004 comparison."""

    key: CanonicalKey
    schema_version: str
    product: ProductState
    pricing: PricingState
    inventory: InventoryState
    expiry: ExpiryState
    promotion_state: PromotionStateData | None
    display_decision: DisplayDecision
    provenance: Provenance
