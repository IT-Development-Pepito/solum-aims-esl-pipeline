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

Every launch also has to take the workflow and store scope before it may run
(FR-009, FR-017). The policy lives in :mod:`esl_service.domain.ownership`; the
lease from #1 enforces it durably. A launch that cannot take the scope creates
no execution at all, so nothing accumulates that no worker will ever start.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from esl_service.domain.actions import ActionState
from esl_service.domain.operations import (
    WORKFLOW_RETRY_REFUSED,
    ReplayRequest,
    RetryRefusalReason,
    RetryRequest,
    decide_retry,
)
from esl_service.domain.outcomes import ExecutionMode, NewExecution, TriggerType
from esl_service.domain.ownership import (
    SCOPE_GRANTED,
    SCOPE_REJECTED,
    SCOPE_RESOURCE,
    OwnershipDecision,
    ScopeOwner,
    decide_ownership,
    scope_key,
)
from esl_service.domain.scheduling import (
    EXECUTION_RESOURCE,
    SCHEDULE_CREATED,
    SCHEDULE_DISABLED,
    SCHEDULE_ENABLED,
    SCHEDULE_RESOURCE,
    SCHEDULER_ACTOR,
    WORKFLOW_LAUNCHED,
    LaunchRefusalReason,
    ManualLaunch,
    ScheduleDefinition,
    build_manual_execution,
    build_scheduled_execution,
    decide_scheduled_launch,
)
from esl_service.domain.serialization import JSONValue
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.models import (
    RecordAction,
    ScopeLease,
    WorkflowExecution,
    WorkflowSchedule,
)
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository


class UnknownSchedule(LookupError):
    """Raised when a schedule change names a schedule that does not exist."""


class UnknownExecution(LookupError):
    """Raised when retry/replay names an execution that does not exist."""


class ScopeContention(RuntimeError):
    """Raised when a scope was taken between the ownership check and the claim.

    The atomic claim, not the check, is the durable guarantee. On the rare
    occasion the two disagree nothing is created: the execution insert is
    rolled back to its savepoint and the caller must retry rather than proceed
    with an execution that owns nothing.
    """


@dataclass(frozen=True)
class LaunchResult:
    """Why one launch attempt did or did not create a run.

    At most one refusal field is set when nothing was created, so a caller
    never has to infer the reason from an absent execution.
    """

    execution: WorkflowExecution | None
    schedule_refusal: LaunchRefusalReason | None = None
    ownership: OwnershipDecision | None = None
    control_refusal: RetryRefusalReason | None = None

    @property
    def launched(self) -> bool:
        """Whether this attempt created a run."""

        return self.execution is not None


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

    def schedules_for_scope(
        self, workflow_name: str, store_code: str | None
    ) -> list[WorkflowSchedule]:
        """Return every schedule of one workflow, or of one store within it.

        The cutover fallback (#26) disables scheduling for a scope; it needs
        the full set, enabled or not, so it can report what it changed and
        what was already off.
        """

        statement = select(WorkflowSchedule).where(
            WorkflowSchedule.workflow_name == workflow_name
        )
        if store_code is not None:
            statement = statement.where(WorkflowSchedule.store_code == store_code)
        return list(self._session.scalars(statement.order_by(WorkflowSchedule.store_code)))

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
        now: datetime | None = None,
    ) -> LaunchResult:
        """Create the run one schedule is due for, if it is due and free.

        Three refusals are ordinary outcomes of a per-minute tick rather than
        errors: the schedule is disabled, the instant is not on its cadence,
        or another execution already owns the scope.
        """

        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            raise UnknownSchedule(f"no schedule with id {schedule_id}")

        definition = _definition_of(schedule)
        decision = decide_scheduled_launch(definition, instant)
        if not decision.should_launch:
            return LaunchResult(execution=None, schedule_refusal=decision.refusal)

        return self._launch(
            build_scheduled_execution(
                definition,
                mode=mode,
                correlation_id=correlation_id,
                source_window_start=source_window_start,
                source_window_end=source_window_end,
                configuration_version_id=configuration_version_id,
                rule_version=rule_version,
            ),
            actor=SCHEDULER_ACTOR,
            reason=f"configured cadence {definition.cron_expression}",
            extra_evidence={"schedule_id": str(schedule.id)},
            now=now or datetime.now(UTC),
        )

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
        now: datetime | None = None,
    ) -> LaunchResult:
        """Create an operator-initiated run carrying identity and reason.

        The run is refused when another execution already owns the scope. A
        manual request does not displace a scheduled owner: the initial policy
        is no simultaneous ownership and no approved priority exists.
        """

        return self._launch(
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
            ),
            actor=launch.requested_by,
            reason=launch.reason,
            now=now or datetime.now(UTC),
        )

    def launch_retry(
        self,
        execution_id: UUID,
        request: RetryRequest,
        *,
        correlation_id: UUID,
        now: datetime | None = None,
    ) -> LaunchResult:
        """Create a linked retry only from an unambiguous FAILED run (FR-011)."""

        original = self._execution(execution_id)
        has_unresolved = (
            self._session.scalar(
                select(RecordAction.id)
                .where(
                    RecordAction.execution_id == original.id,
                    RecordAction.state == ActionState.OUTCOME_UNKNOWN.value,
                )
                .limit(1)
            )
            is not None
        )
        decision = decide_retry(
            ExecutionStatus(original.status),
            has_unresolved_external_action=has_unresolved,
        )
        if not decision.allowed:
            assert decision.refusal is not None
            self._record_retry_refusal(
                original,
                request=request,
                correlation_id=correlation_id,
                refusal=decision.refusal,
                has_unresolved_external_action=has_unresolved,
            )
            return LaunchResult(execution=None, control_refusal=decision.refusal)

        return self._launch(
            NewExecution(
                workflow_name=original.workflow_name,
                store_code=original.store_code,
                trigger_type=TriggerType.RETRY,
                mode=ExecutionMode(original.mode),
                correlation_id=correlation_id,
                source_window_start=original.source_window_start,
                source_window_end=original.source_window_end,
                configuration_version_id=original.configuration_version_id,
                rule_version=original.rule_version,
                requested_by=request.requested_by,
                reason=request.reason,
                retry_of_execution_id=original.id,
            ),
            actor=request.requested_by,
            reason=request.reason,
            extra_evidence={"retry_of_execution_id": str(original.id)},
            now=now or datetime.now(UTC),
        )

    def launch_replay(
        self,
        execution_id: UUID,
        request: ReplayRequest,
        *,
        correlation_id: UUID,
        now: datetime | None = None,
    ) -> LaunchResult:
        """Create a linked replay for exactly one validated source window."""

        original = self._execution(execution_id)
        assert request.source_window_start is not None
        assert request.source_window_end is not None
        return self._launch(
            NewExecution(
                workflow_name=original.workflow_name,
                store_code=original.store_code,
                trigger_type=TriggerType.REPLAY,
                mode=ExecutionMode(original.mode),
                correlation_id=correlation_id,
                source_window_start=request.source_window_start,
                source_window_end=request.source_window_end,
                configuration_version_id=original.configuration_version_id,
                rule_version=original.rule_version,
                requested_by=request.requested_by,
                reason=request.reason,
                replay_of_execution_id=original.id,
            ),
            actor=request.requested_by,
            reason=request.reason,
            extra_evidence={
                "replay_of_execution_id": str(original.id),
                "source_window_start": request.source_window_start.isoformat(),
                "source_window_end": request.source_window_end.isoformat(),
            },
            now=now or datetime.now(UTC),
        )

    # --- scope ownership ---------------------------------------------------

    def current_owner(self, scope: str) -> ScopeOwner | None:
        """Return who currently holds one scope, as the policy sees it.

        The owner's trigger type comes from its execution, so a refusal can
        say what kind of work it lost to (FR-017).
        """

        statement = (
            select(ScopeLease, WorkflowExecution.trigger_type)
            .join(WorkflowExecution, ScopeLease.execution_id == WorkflowExecution.id)
            .where(ScopeLease.scope_key == scope)
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None

        lease, trigger_type = row
        return ScopeOwner(
            execution_id=lease.execution_id,
            trigger_type=TriggerType(trigger_type),
            expires_at=lease.expires_at,
            released=lease.released_at is not None,
        )

    def _execution(self, execution_id: UUID) -> WorkflowExecution:
        execution = self._session.get(WorkflowExecution, execution_id)
        if execution is None:
            raise UnknownExecution(f"no execution with id {execution_id}")
        return execution

    def _record_retry_refusal(
        self,
        original: WorkflowExecution,
        *,
        request: RetryRequest,
        correlation_id: UUID,
        refusal: RetryRefusalReason,
        has_unresolved_external_action: bool,
    ) -> None:
        """Audit a refused operator retry even though it creates no new run."""

        self._audit.append_audit_entry(
            actor=request.requested_by,
            action=WORKFLOW_RETRY_REFUSED,
            reason=request.reason,
            resource_type=EXECUTION_RESOURCE,
            resource_key=str(original.id),
            outcome=refusal.value,
            execution_id=original.id,
            configuration_version_id=original.configuration_version_id,
            correlation_id=correlation_id,
            after_evidence={
                "reason_code": refusal.value,
                "status": original.status,
                "has_unresolved_external_action": has_unresolved_external_action,
            },
        )

    def _launch(
        self,
        request: NewExecution,
        *,
        actor: str,
        reason: str,
        now: datetime,
        extra_evidence: dict[str, JSONValue] | None = None,
    ) -> LaunchResult:
        """Take the scope, then create and audit the run it authorises."""

        scope = scope_key(request.workflow_name, request.store_code)
        decision = decide_ownership(
            scope,
            request.trigger_type,
            current=self.current_owner(scope),
            now=now,
        )
        if not decision.granted:
            self._record_ownership(decision, actor=actor, reason=reason)
            return LaunchResult(execution=None, ownership=decision)

        # The execution insert is undone if the atomic claim loses a race, so
        # no run ever exists without owning the scope it was created for.
        savepoint = self._session.begin_nested()
        execution = self._executions.create_execution(request, now=now)
        if not self._executions.claim_scope(execution.id, scope, now=now):
            savepoint.rollback()
            raise ScopeContention(f"scope {scope} was taken during the launch")
        savepoint.commit()

        self._record_ownership(decision, actor=actor, reason=reason, execution=execution)
        self._record_launch(
            execution, actor=actor, reason=reason, extra_evidence=extra_evidence
        )
        return LaunchResult(execution=execution, ownership=decision)

    def _record_ownership(
        self,
        decision: OwnershipDecision,
        *,
        actor: str,
        reason: str,
        execution: WorkflowExecution | None = None,
    ) -> None:
        """Audit one ownership decision, its owner, and its outcome (FR-009)."""

        self._audit.append_audit_entry(
            actor=actor,
            action=SCOPE_GRANTED if decision.granted else SCOPE_REJECTED,
            reason=reason,
            resource_type=SCOPE_RESOURCE,
            resource_key=decision.scope_key,
            outcome=decision.outcome.value,
            execution_id=execution.id if execution else None,
            configuration_version_id=(
                execution.configuration_version_id if execution else None
            ),
            correlation_id=execution.correlation_id if execution else None,
            after_evidence=decision.evidence(),
        )

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
