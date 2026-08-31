"""Schedule configuration and auditable workflow launch (FR-008).

The launch decision itself is the pure-domain contract in
:mod:`esl_service.domain.scheduling`; this layer persists its result and
appends the audit evidence in the same transaction. No method commits a
caller's transaction.

Three things are audited because FR-008 requires schedule configuration,
enable/disable changes, and launch source to be visible without parsing logs:
creating a schedule, changing whether it is enabled, and every launch that
actually creates an execution. Refusals are deliberately *not* audited. A
schedule is evaluated once a minute, so recording every not-due or disabled
tick would bury the operator actions that FR-022 exists to surface, and the
enable/disable entry already explains why nothing ran.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import (
    EXECUTION_RESOURCE,
    SCHEDULE_CREATED,
    SCHEDULE_DISABLED,
    SCHEDULE_ENABLED,
    SCHEDULE_RESOURCE,
    SCHEDULER_ACTOR,
    WORKFLOW_LAUNCHED,
    ManualLaunch,
    ScheduleDefinition,
    build_manual_execution,
    build_scheduled_execution,
    decide_scheduled_launch,
)
from esl_service.domain.serialization import JSONValue
from esl_service.persistence.models import WorkflowExecution, WorkflowSchedule
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository


class UnknownSchedule(LookupError):
    """Raised when a schedule change names a schedule that does not exist."""


def _definition_of(row: WorkflowSchedule) -> ScheduleDefinition:
    """Return the domain definition a stored schedule represents.

    Constructing it revalidates the cadence and timezone, so a row that became
    unreadable is refused here rather than silently never firing.
    """

    return ScheduleDefinition(
        workflow_name=row.workflow_name,
        store_code=row.store_code,
        cron_expression=row.cron_expression,
        timezone=row.timezone,
        enabled=row.enabled,
    )


class LaunchRepository:
    """Persists schedules and the executions they and operators launch."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._executions = ExecutionRepository(session)
        self._audit = ReconciliationRepository(session)

    # --- schedule configuration -------------------------------------------

    def create_schedule(
        self,
        definition: ScheduleDefinition,
        *,
        configuration_version_id: UUID,
        actor: str,
        reason: str,
    ) -> WorkflowSchedule:
        """Store one configured cadence and audit its full configuration."""

        schedule = WorkflowSchedule(
            workflow_name=definition.workflow_name,
            store_code=definition.store_code,
            cron_expression=definition.cron_expression,
            timezone=definition.timezone,
            enabled=definition.enabled,
            configuration_version_id=configuration_version_id,
        )
        self._session.add(schedule)
        self._session.flush()

        self._audit.append_audit_entry(
            actor=actor,
            action=SCHEDULE_CREATED,
            reason=reason,
            resource_type=SCHEDULE_RESOURCE,
            resource_key=str(schedule.id),
            outcome="CREATED",
            configuration_version_id=configuration_version_id,
            after_evidence={
                "workflow_name": definition.workflow_name,
                "store_code": definition.store_code,
                "cron_expression": definition.cron_expression,
                "timezone": definition.timezone,
                "enabled": definition.enabled,
            },
        )
        return schedule

    def get_schedule(self, schedule_id: UUID) -> WorkflowSchedule | None:
        """Return one stored schedule, or None when it does not exist."""

        return self._session.get(WorkflowSchedule, schedule_id)

    def set_schedule_enabled(
        self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str
    ) -> WorkflowSchedule:
        """Enable or disable one schedule and audit the change (FR-008)."""

        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise UnknownSchedule(f"no schedule with id {schedule_id}")

        was_enabled = schedule.enabled
        schedule.enabled = enabled
        self._session.flush()

        self._audit.append_audit_entry(
            actor=actor,
            action=SCHEDULE_ENABLED if enabled else SCHEDULE_DISABLED,
            reason=reason,
            resource_type=SCHEDULE_RESOURCE,
            resource_key=str(schedule.id),
            outcome="APPLIED",
            configuration_version_id=schedule.configuration_version_id,
            before_evidence={"enabled": was_enabled},
            after_evidence={"enabled": enabled},
        )
        return schedule

    def due_schedules(self, instant: datetime) -> list[WorkflowSchedule]:
        """Return every schedule that should launch at one instant.

        Disabled schedules are excluded by the same domain decision that
        refuses an individual launch, so the two cannot disagree.
        """

        statement = select(WorkflowSchedule).order_by(
            WorkflowSchedule.workflow_name, WorkflowSchedule.store_code
        )
        return [
            row
            for row in self._session.scalars(statement)
            if decide_scheduled_launch(_definition_of(row), instant).should_launch
        ]

    # --- launching ---------------------------------------------------------

    def launch_scheduled(
        self,
        schedule_id: UUID,
        *,
        instant: datetime,
        mode: ExecutionMode,
        correlation_id: UUID,
        source_window_start: datetime,
        source_window_end: datetime,
        configuration_version_id: UUID,
        rule_version: str,
    ) -> WorkflowExecution | None:
        """Create the run one schedule is due for, or None when it is not.

        None means the domain refused: the schedule is disabled, or the
        instant is not on its cadence. Both are ordinary outcomes of a
        per-minute tick rather than errors.
        """

        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise UnknownSchedule(f"no schedule with id {schedule_id}")

        definition = _definition_of(schedule)
        if not decide_scheduled_launch(definition, instant).should_launch:
            return None

        execution = self._executions.create_execution(
            build_scheduled_execution(
                definition,
                mode=mode,
                correlation_id=correlation_id,
                source_window_start=source_window_start,
                source_window_end=source_window_end,
                configuration_version_id=configuration_version_id,
                rule_version=rule_version,
            )
        )
        self._record_launch(
            execution,
            actor=SCHEDULER_ACTOR,
            reason=f"configured cadence {definition.cron_expression}",
            extra_evidence={"schedule_id": str(schedule.id)},
        )
        return execution

    def launch_manual(
        self,
        launch: ManualLaunch,
        *,
        workflow_name: str,
        store_code: str,
        mode: ExecutionMode,
        correlation_id: UUID,
        source_window_start: datetime,
        source_window_end: datetime,
        configuration_version_id: UUID,
        rule_version: str,
    ) -> WorkflowExecution:
        """Create an operator-initiated run carrying identity and reason."""

        execution = self._executions.create_execution(
            build_manual_execution(
                launch,
                workflow_name=workflow_name,
                store_code=store_code,
                mode=mode,
                correlation_id=correlation_id,
                source_window_start=source_window_start,
                source_window_end=source_window_end,
                configuration_version_id=configuration_version_id,
                rule_version=rule_version,
            )
        )
        self._record_launch(execution, actor=launch.requested_by, reason=launch.reason)
        return execution

    def _record_launch(
        self,
        execution: WorkflowExecution,
        *,
        actor: str,
        reason: str,
        extra_evidence: dict[str, JSONValue] | None = None,
    ) -> None:
        """Audit one launch so its source is visible without parsing logs."""

        evidence: dict[str, JSONValue] = {"trigger_type": execution.trigger_type}
        evidence.update(extra_evidence or {})
        self._audit.append_audit_entry(
            actor=actor,
            action=WORKFLOW_LAUNCHED,
            reason=reason,
            resource_type=EXECUTION_RESOURCE,
            resource_key=str(execution.id),
            outcome="QUEUED",
            execution_id=execution.id,
            configuration_version_id=execution.configuration_version_id,
            correlation_id=execution.correlation_id,
            after_evidence=evidence,
        )
