"""Authorized manual operations against real rows (FR-023, #26).

The unit tests prove the service's decisions with fakes; these prove that the
existing repositories satisfy the service's ports unchanged, that a refusal
lands in ``audit_entry``, and that fallback flips real ``workflow_schedule``
rows while leaving every execution untouched.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from esl_service.application.operations import AuthorizedOperations
from esl_service.domain.authorization import (
    FALLBACK_APPLIED,
    OPERATION_REFUSED,
    NotAuthorized,
    Principal,
    Role,
)
from esl_service.domain.operations import ExecutionQuery
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import SCHEDULE_DISABLED, ScheduleDefinition
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.models import (
    AuditEntry,
    WorkflowExecution,
    WorkflowSchedule,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository

LEGACY_CADENCE = "*/30 7-23 * * *"
WINDOW_START = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)

OPERATOR = Principal("ops.alice", frozenset({Role.OPERATOR}))
ADMIN = Principal("ops.root", frozenset({Role.ADMIN}))
NOBODY = Principal("guest", frozenset())


@pytest.fixture
def service(session: Session) -> AuthorizedOperations:
    """Wire the service to the real repositories on the rolled-back transaction."""

    launches = LaunchRepository(session)
    return AuthorizedOperations(
        launches=launches,
        schedules=launches,
        status=ExecutionRepository(session),
        reconciliation=ReconciliationRepository(session),
        audit=ReconciliationRepository(session),
    )


def schedule(
    session: Session, configuration_version_id: UUID, store_code: str, *, enabled: bool = True
) -> WorkflowSchedule:
    return LaunchRepository(session).create_schedule(
        ScheduleDefinition(
            workflow_name="esl-refresh",
            store_code=store_code,
            cron_expression=LEGACY_CADENCE,
            timezone="Asia/Jakarta",
            enabled=enabled,
        ),
        configuration_version_id=configuration_version_id,
        actor="ops.root",
        reason="CHG-1 configuration",
    )


def audit_actions(session: Session, *, actor: str) -> list[str]:
    statement = (
        select(AuditEntry.action).where(AuditEntry.actor == actor).order_by(AuditEntry.sequence)
    )
    return list(session.scalars(statement))


def execution_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(WorkflowExecution)) or 0


def test_an_operator_trigger_creates_a_run_carrying_identity_and_reason(
    service: AuthorizedOperations, session: Session, configuration_version_id: UUID
) -> None:
    result = service.trigger(
        OPERATOR,
        "CHG-2 manual refresh",
        workflow_name="esl-refresh",
        store_code="084",
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        configuration_version_id=configuration_version_id,
        rule_version="rules-v1",
    )

    assert result.launched
    assert result.execution is not None
    assert result.execution.requested_by == "ops.alice"
    assert result.execution.reason == "CHG-2 manual refresh"


def test_a_refused_operation_is_an_audit_row_and_creates_no_run(
    service: AuthorizedOperations, session: Session, configuration_version_id: UUID
) -> None:
    with pytest.raises(NotAuthorized):
        service.trigger(
            NOBODY,
            "CHG-3",
            workflow_name="esl-refresh",
            store_code="084",
            mode=ExecutionMode.SHADOW,
            correlation_id=uuid4(),
            source_window_start=WINDOW_START,
            source_window_end=WINDOW_END,
            configuration_version_id=configuration_version_id,
            rule_version="rules-v1",
        )

    assert execution_count(session) == 0
    assert audit_actions(session, actor="guest") == [OPERATION_REFUSED]


def test_status_reads_through_the_execution_repository(
    service: AuthorizedOperations, configuration_version_id: UUID
) -> None:
    service.trigger(
        OPERATOR,
        "CHG-4",
        workflow_name="esl-refresh",
        store_code="084",
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        configuration_version_id=configuration_version_id,
        rule_version="rules-v1",
    )

    found = service.status(OPERATOR, ExecutionQuery(store_code="084"))

    assert len(found) == 1


def test_fallback_disables_real_schedules_and_preserves_every_execution(
    service: AuthorizedOperations, session: Session, configuration_version_id: UUID
) -> None:
    on_084 = schedule(session, configuration_version_id, "084")
    on_075 = schedule(session, configuration_version_id, "075")
    service.trigger(
        OPERATOR,
        "CHG-5",
        workflow_name="esl-refresh",
        store_code="084",
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        configuration_version_id=configuration_version_id,
        rule_version="rules-v1",
    )
    executions_before = execution_count(session)

    outcome = service.fallback(ADMIN, "INC-9 cutover rollback", workflow_name="esl-refresh")

    session.expire_all()
    assert set(outcome.disabled_schedule_ids) == {on_084.id, on_075.id}
    assert session.get(WorkflowSchedule, on_084.id).enabled is False  # type: ignore[union-attr]
    assert session.get(WorkflowSchedule, on_075.id).enabled is False  # type: ignore[union-attr]
    assert execution_count(session) == executions_before
    actions = audit_actions(session, actor="ops.root")
    assert actions.count(SCHEDULE_DISABLED) == 2
    assert actions[-1] == FALLBACK_APPLIED


def test_fallback_is_refused_for_an_operator_and_flips_nothing(
    service: AuthorizedOperations, session: Session, configuration_version_id: UUID
) -> None:
    on_084 = schedule(session, configuration_version_id, "084")

    with pytest.raises(NotAuthorized):
        service.fallback(OPERATOR, "INC-9", workflow_name="esl-refresh")

    session.expire_all()
    assert session.get(WorkflowSchedule, on_084.id).enabled is True  # type: ignore[union-attr]
    assert audit_actions(session, actor="ops.alice") == [OPERATION_REFUSED]
