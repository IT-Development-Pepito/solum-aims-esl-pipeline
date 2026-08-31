"""SQLAlchemy models for the service-owned PostgreSQL state schema.

Every mapped class is re-exported here so ``esl_service.persistence.models``
stays a stable import path and ``Base.metadata`` remains complete for Alembic.
"""

from esl_service.persistence.models.base import Base
from esl_service.persistence.models.configuration import (
    ConfigurationVersion,
    StoreConfiguration,
)
from esl_service.persistence.models.evidence import (
    CanonicalRecordSnapshot,
    PromotionCandidateSnapshot,
    PromotionEvaluation,
    RecordDifference,
    SnapshotSet,
)
from esl_service.persistence.models.execution import (
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionStep,
    RecordAction,
    ScopeLease,
    WorkflowExecution,
    WorkflowSchedule,
)
from esl_service.persistence.models.outcomes import RecordIssue, RecordProcessingResult

__all__ = [
    "Base",
    "CanonicalRecordSnapshot",
    "ConfigurationVersion",
    "ExecutionCheckpoint",
    "ExecutionEvent",
    "ExecutionStep",
    "PromotionCandidateSnapshot",
    "PromotionEvaluation",
    "RecordAction",
    "RecordDifference",
    "RecordIssue",
    "RecordProcessingResult",
    "ScopeLease",
    "SnapshotSet",
    "StoreConfiguration",
    "WorkflowExecution",
    "WorkflowSchedule",
]
