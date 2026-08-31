"""Integration coverage for persisted record outcomes and issues (FR-003, FR-006).

Rejected and unresolved records are counted and traceable, and every issue is
queryable independently rather than flattened into one message.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from esl_service.domain.canonical import CanonicalKey
from esl_service.domain.outcomes import (
    ActionDecision,
    EligibilityStatus,
    ProcessingStatus,
    RecordIssueEvidence,
    RecordProcessingEvidence,
    ValidationStatus,
)
from esl_service.domain.promotion_evidence import PromotionOutcome
from esl_service.persistence.evidence_repository import RecordOutcomeRepository
from esl_service.persistence.models import RecordProcessingResult
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from tests.factories import canonical_record, new_execution

KEY = CanonicalKey("084", "101024011793", "KGS")


@pytest.fixture
def execution_id(
    session: Session,
    execution_repository: ExecutionRepository,
    configuration_version_id: UUID,
) -> UUID:
    """Create the execution the record outcomes belong to."""

    execution = execution_repository.create_execution(
        new_execution(configuration_version_id)
    )
    session.flush()
    return execution.id


@pytest.fixture
def snapshot_id(
    session: Session,
    snapshot_repository: SnapshotRepository,
    execution_id: UUID,
) -> UUID:
    """Persist the canonical snapshot the outcome refers to."""

    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution_id,
        representation_kind="SOURCE_EXPECTED",
        adapter_name="sqlserver",
        source_watermark="2026-08-28T07:00:00+00:00",
        canonical_schema_version="canonical-v1",
    )
    record = snapshot_repository.append_record(snapshot_set.id, canonical_record())
    session.flush()
    return record.id


def issue(rule_id: str, issue_code: str) -> RecordIssueEvidence:
    """Build one issue record."""

    return RecordIssueEvidence(
        rule_id=rule_id,
        issue_code=issue_code,
        severity="ERROR",
        classification="PROMOTION",
        evidence={"source_campaign_id": "A"},
    )


def processing(**overrides: object) -> RecordProcessingEvidence:
    """Build one record outcome, unresolved by default."""

    values: dict[str, object] = {
        "key": KEY,
        "validation_status": ValidationStatus.VALID,
        "eligibility_status": EligibilityStatus.UNRESOLVED,
        "promotion_outcome": PromotionOutcome.UNRESOLVED,
        "current_page": 1,
        "desired_page": 2,
        "action_decision": ActionDecision.NONE,
        "processing_status": ProcessingStatus.UNRESOLVED,
        "issues": (
            issue("BR-013", "UOM_RULE_REQUIRED"),
            issue("BR-006", "CATEGORY_001_PRICE_MISSING"),
        ),
    }
    values.update(overrides)
    return RecordProcessingEvidence(**values)  # type: ignore[arg-type]


def test_record_retains_multiple_independent_issues(
    session: Session,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Each issue is its own queryable row (FR-006, FR-022)."""

    stored = outcome_repository.record_processing_result(
        execution_id, snapshot_id, processing()
    )
    session.flush()

    assert [row.issue_code for row in stored.issues] == [
        "UOM_RULE_REQUIRED",
        "CATEGORY_001_PRICE_MISSING",
    ]
    assert stored.processing_status == "UNRESOLVED"
    assert stored.issues[0].evidence == {"source_campaign_id": "A"}


def test_result_is_unique_per_execution_and_key(
    session: Session,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """One record has one outcome per execution."""

    outcome_repository.record_processing_result(execution_id, snapshot_id, processing())
    session.flush()

    with pytest.raises(IntegrityError):
        outcome_repository.record_processing_result(
            execution_id, snapshot_id, processing()
        )
        session.flush()


def test_the_canonical_key_is_relationally_queryable(
    session: Session,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Store, item, and selling UOM are columns, not buried in JSON (BR-018)."""

    outcome_repository.record_processing_result(execution_id, snapshot_id, processing())
    session.flush()

    found = session.scalars(
        select(RecordProcessingResult).where(
            RecordProcessingResult.store_code == "084",
            RecordProcessingResult.selling_uom == "KGS",
        )
    ).all()
    assert [row.item_code for row in found] == ["101024011793"]


def test_mixed_outcomes_are_counted_by_status(
    session: Session,
    snapshot_repository: SnapshotRepository,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Rejected and unresolved records are countable for reconciliation."""

    snapshot_set = snapshot_repository.create_snapshot_set(
        execution_id=execution_id,
        representation_kind="AIMS_OBSERVED",
        adapter_name="aims-read",
        source_watermark="2026-08-28T07:00:00+00:00",
        canonical_schema_version="canonical-v1",
    )
    other = snapshot_repository.append_record(
        snapshot_set.id, canonical_record(store_code="075")
    )
    session.flush()

    outcome_repository.record_processing_result(execution_id, snapshot_id, processing())
    outcome_repository.record_processing_result(
        execution_id,
        other.id,
        processing(
            key=CanonicalKey("075", "101024011793", "KGS"),
            eligibility_status=EligibilityStatus.ELIGIBLE,
            promotion_outcome=PromotionOutcome.SELECTED,
            action_decision=ActionDecision.PAGE_CHANGE,
            processing_status=ProcessingStatus.ACTION_REQUIRED,
            issues=(),
        ),
    )
    session.flush()

    counts = dict(
        session.execute(
            select(
                RecordProcessingResult.processing_status,
                func.count(),
            )
            .where(RecordProcessingResult.execution_id == execution_id)
            .group_by(RecordProcessingResult.processing_status)
        ).all()
    )
    assert counts == {"UNRESOLVED": 1, "ACTION_REQUIRED": 1}


def test_a_clean_record_stores_no_issue(
    session: Session,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """An unchanged record is not an anomaly and carries no issue row."""

    stored = outcome_repository.record_processing_result(
        execution_id,
        snapshot_id,
        processing(
            eligibility_status=EligibilityStatus.ELIGIBLE,
            promotion_outcome=PromotionOutcome.NO_PROMOTION,
            current_page=2,
            desired_page=2,
            action_decision=ActionDecision.SKIP_IDEMPOTENT,
            processing_status=ProcessingStatus.UNCHANGED,
            issues=(),
        ),
    )
    session.flush()

    assert stored.issues == []


def test_result_with_issues_cannot_be_deleted(
    session: Session,
    outcome_repository: RecordOutcomeRepository,
    execution_id: UUID,
    snapshot_id: UUID,
) -> None:
    """Durable evidence uses RESTRICT so quarantine history survives."""

    stored = outcome_repository.record_processing_result(
        execution_id, snapshot_id, processing()
    )
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            delete(RecordProcessingResult).where(
                RecordProcessingResult.id == stored.id
            )
        )
        session.flush()


def test_list_results_returns_none_for_an_unknown_execution(
    outcome_repository: RecordOutcomeRepository,
) -> None:
    """An execution with no outcomes returns an empty list, not an error."""

    assert outcome_repository.list_results(uuid4()) == []
