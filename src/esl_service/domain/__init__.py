"""Pure canonical domain contracts for deterministic ESL processing."""

from esl_service.domain.canonical import (
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
from esl_service.domain.diff import FieldDifference, diff_payloads, diff_records
from esl_service.domain.serialization import canonical_hash, canonical_payload

__all__ = [
    "CanonicalEslRecord",
    "CanonicalKey",
    "DisplayDecision",
    "ExpiryState",
    "FieldDifference",
    "InventoryState",
    "PriceBasis",
    "PricingState",
    "ProductState",
    "PromotionStateData",
    "Provenance",
    "canonical_hash",
    "canonical_payload",
    "diff_payloads",
    "diff_records",
]
