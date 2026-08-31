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
from esl_service.domain.workflow import (
    ExecutionStatus,
    InvalidWorkflowTransition,
    StepDependency,
    StepOutcome,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTransitionEvent,
    is_terminal,
    transition_execution,
)

__all__ = [
    "CanonicalEslRecord",
    "CanonicalKey",
    "DisplayDecision",
    "ExecutionStatus",
    "ExpiryState",
    "FieldDifference",
    "InvalidWorkflowTransition",
    "InventoryState",
    "PriceBasis",
    "PricingState",
    "ProductState",
    "PromotionStateData",
    "Provenance",
    "StepDependency",
    "StepOutcome",
    "WorkflowDefinition",
    "WorkflowStep",
    "WorkflowTransitionEvent",
    "canonical_hash",
    "canonical_payload",
    "diff_payloads",
    "diff_records",
    "is_terminal",
    "transition_execution",
]
