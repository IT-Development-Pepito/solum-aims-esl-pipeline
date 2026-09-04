"""Authorized manual operations (FR-023, #26).

One service fronts every operation FR-023 lists -- trigger, status, retry,
replay, schedule enable/disable, reconciliation, fallback -- and does three
things before delegating to persistence: it checks the principal's role
(AD-018), it insists on a reason for every mutation, and it writes a refusal
to the audit ledger under the principal's own name. Launches, retries,
replays, and schedule changes are already audited by the repositories they
delegate to, so the service adds an entry only where none would otherwise
exist: a refusal, a reconciliation request, and a fallback.

The ports below are the *shapes* of the existing repositories, not new
abstractions: ``LaunchRepository`` satisfies ``LaunchPort`` and
``SchedulePort``, ``ExecutionRepository`` satisfies ``StatusPort``, and
``ReconciliationRepository`` satisfies both ``ReconciliationPort`` and
``AuditPort``. Stating them here keeps this module free of persistence
imports (architecture boundary tests) and lets the unit tests use fakes.

Fallback is the in-application part of the cutover rollback SPECIFICATION
section 8 defines: disable target scheduling for the scope, preserve every
execution and audit row, and record the instant from which the cutover window
must be reconciled. Restoring the legacy SQL Agent schedule and opening the
incident record happen outside this service and are not automated.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from esl_service.domain.authorization import (
    AUTHORIZATION_RESOURCE,
    FALLBACK_APPLIED,
    FALLBACK_RESOURCE,
    OPERATION_REFUSED,
    RECONCILIATION_REQUESTED,
    NotAuthorized,
    Operation,
    Principal,
    authorize,
)
from esl_service.domain.operations import (
    ExecutionQuery,
    InvalidWorkflowControl,
    ReplayRequest,
    RetryRequest,
    SnapshotReplayRequest,
)
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.scheduling import InvalidManualLaunch, ManualLaunch
from esl_service.domain.serialization import JSONValue

#: Resource type recorded on a reconciliation request; the report itself is
#: keyed by execution, so the request names the execution too.
RECONCILIATION_RESOURCE = "reconciliation"


class InvalidOperationRequest(ValueError):
    """Raised when a request is malformed before any role check or delegation."""


# --- ports (the repositories' shapes) ----------------------------------------


class ScheduleView(Protocol):
    """What the service needs to know about one stored schedule."""

    @property
    def id(self) -> UUID: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def workflow_name(self) -> str: ...

    @property
    def store_code(self) -> str: ...


class LaunchPort(Protocol):
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
    ) -> object: ...

    def launch_retry(
        self, execution_id: UUID, request: RetryRequest, *, correlation_id: UUID
    ) -> object: ...

    def launch_replay(
        self, execution_id: UUID, request: ReplayRequest, *, correlation_id: UUID
    ) -> object: ...

    def launch_snapshot_replay(
        self, execution_id: UUID, request: SnapshotReplayRequest, *, correlation_id: UUID
    ) -> object: ...


class SchedulePort(Protocol):
    def schedules_for_scope(
        self, workflow_name: str, store_code: str | None
    ) -> Sequence[ScheduleView]: ...

    def set_schedule_enabled(
        self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str
    ) -> ScheduleView: ...


class StatusPort(Protocol):
    def query_executions(self, query: ExecutionQuery) -> Sequence[object]: ...


class ReconciliationPort(Protocol):
    def finalize_report(
        self, execution_id: UUID, mode: ReconciliationMode, counts: ReconciliationCounts
    ) -> object: ...


class AuditPort(Protocol):
    def append_audit_entry(
        self,
        *,
        actor: str,
        action: str,
        reason: str,
        resource_type: str,
        resource_key: str,
        outcome: str,
        execution_id: UUID | None = None,
        configuration_version_id: UUID | None = None,
        correlation_id: UUID | None = None,
        before_evidence: Mapping[str, JSONValue] | None = None,
        after_evidence: Mapping[str, JSONValue] | None = None,
    ) -> object: ...


# --- outcomes -----------------------------------------------------------------


@dataclass(frozen=True)
class FallbackOutcome:
    """What one fallback did, so the operator sees it without the ledger."""

    disabled_schedule_ids: tuple[UUID, ...]
    already_disabled: tuple[UUID, ...]
    applied_at: datetime


# --- the service --------------------------------------------------------------


def _require_reason(reason: str) -> None:
    if not reason.strip():
        raise InvalidOperationRequest("reason must not be blank")


class AuthorizedOperations:
    """Role-checked, reason-bearing, audited manual operations (FR-023)."""

    def __init__(
        self,
        *,
        launches: LaunchPort,
        schedules: SchedulePort,
        status: StatusPort,
        reconciliation: ReconciliationPort,
        audit: AuditPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._launches = launches
        self._schedules = schedules
        self._status = status
        self._reconciliation = reconciliation
        self._audit = audit
        self._clock = clock

    # -- authorization -------------------------------------------------------

    def authorize(self, principal: Principal, operation: Operation, *, resource_key: str) -> None:
        """Check a role for a caller-owned action, auditing and raising on refusal.

        Used by surfaces (#28) that hold state the service does not, such as
        the scheduler's pause flag, so their refusals land in the same ledger.
        """

        self._authorize(principal, operation, resource_key)

    def _authorize(self, principal: Principal, operation: Operation, resource_key: str) -> None:
        """Refuse, audit the refusal under the principal's name, and raise."""

        decision = authorize(principal, operation)
        if decision.allowed:
            return
        held_roles: list[JSONValue] = [role.value for role in sorted(principal.roles)]
        self._audit.append_audit_entry(
            actor=principal.identity,
            action=OPERATION_REFUSED,
            reason=f"role {decision.required_role.value} is required",
            resource_type=AUTHORIZATION_RESOURCE,
            resource_key=resource_key,
            outcome="REFUSED",
            after_evidence={
                "operation": operation.value,
                "required_role": decision.required_role.value,
                "held_roles": held_roles,
                "policy_version": decision.policy_version,
            },
        )
        raise NotAuthorized(decision)

    # -- operations ----------------------------------------------------------

    def trigger(
        self,
        principal: Principal,
        reason: str,
        *,
        workflow_name: str,
        store_code: str,
        mode: ExecutionMode,
        correlation_id: UUID,
        source_window_start: datetime,
        source_window_end: datetime,
        configuration_version_id: UUID,
        rule_version: str,
    ) -> object:
        """Launch a run under the principal's identity and reason (FR-008)."""

        _require_reason(reason)
        self._authorize(principal, Operation.TRIGGER, f"{workflow_name}/{store_code}")
        try:
            launch = ManualLaunch(requested_by=principal.identity, reason=reason)
        except InvalidManualLaunch as error:
            raise InvalidOperationRequest(str(error)) from None
        return self._launches.launch_manual(
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

    def status(self, principal: Principal, query: ExecutionQuery) -> Sequence[object]:
        """Read execution status; a read needs a role but not a reason (FR-012)."""

        self._authorize(principal, Operation.STATUS, "executions")
        return self._status.query_executions(query)

    def retry(
        self, principal: Principal, execution_id: UUID, reason: str, *, correlation_id: UUID
    ) -> object:
        """Create a linked retry of a FAILED run (FR-011)."""

        _require_reason(reason)
        self._authorize(principal, Operation.RETRY, str(execution_id))
        request = self._control(RetryRequest, requested_by=principal.identity, reason=reason)
        return self._launches.launch_retry(execution_id, request, correlation_id=correlation_id)

    def replay(
        self,
        principal: Principal,
        execution_id: UUID,
        reason: str,
        *,
        correlation_id: UUID,
        source_window_start: datetime | None,
        source_window_end: datetime | None,
    ) -> object:
        """Create a linked replay of exactly one validated window (FR-011)."""

        _require_reason(reason)
        request = self._control(
            ReplayRequest,
            requested_by=principal.identity,
            reason=reason,
            source_window_start=source_window_start,
            source_window_end=source_window_end,
        )
        self._authorize(principal, Operation.REPLAY, str(execution_id))
        return self._launches.launch_replay(execution_id, request, correlation_id=correlation_id)

    def replay_snapshot(
        self, principal: Principal, execution_id: UUID, reason: str, *, correlation_id: UUID
    ) -> object:
        """Reproduce a run from its retained capture, reading no source (#114).

        The same replay role applies, but no window is taken: the retained
        capture's own window, configuration, and rule version are the input.
        """

        _require_reason(reason)
        request = self._control(
            SnapshotReplayRequest, requested_by=principal.identity, reason=reason
        )
        self._authorize(principal, Operation.REPLAY, str(execution_id))
        return self._launches.launch_snapshot_replay(
            execution_id, request, correlation_id=correlation_id
        )

    def enable_schedule(self, principal: Principal, schedule_id: UUID, reason: str) -> object:
        return self._set_schedule(principal, schedule_id, reason, enabled=True)

    def disable_schedule(self, principal: Principal, schedule_id: UUID, reason: str) -> object:
        return self._set_schedule(principal, schedule_id, reason, enabled=False)

    def reconcile(
        self,
        principal: Principal,
        execution_id: UUID,
        reason: str,
        *,
        mode: ReconciliationMode,
        counts: ReconciliationCounts,
    ) -> object:
        """Finalize a reconciliation revision at an operator's request (FR-021)."""

        _require_reason(reason)
        self._authorize(principal, Operation.RECONCILE, str(execution_id))
        report = self._reconciliation.finalize_report(execution_id, mode, counts)
        self._audit.append_audit_entry(
            actor=principal.identity,
            action=RECONCILIATION_REQUESTED,
            reason=reason,
            resource_type=RECONCILIATION_RESOURCE,
            resource_key=str(execution_id),
            outcome="FINALIZED",
            execution_id=execution_id,
            after_evidence={"mode": mode.value},
        )
        return report

    def fallback(
        self,
        principal: Principal,
        reason: str,
        *,
        workflow_name: str,
        store_code: str | None = None,
    ) -> FallbackOutcome:
        """Disable target scheduling for a scope and record the rollback instant.

        Executions, checkpoints, and audit rows are never touched: rollback
        preserves target evidence (SPECIFICATION section 8). Each schedule
        change is audited by the repository; one ``fallback.applied`` entry
        ties them together and names the instant from which the cutover
        window must be reconciled.
        """

        _require_reason(reason)
        resource_key = workflow_name if store_code is None else f"{workflow_name}/{store_code}"
        self._authorize(principal, Operation.FALLBACK, resource_key)

        applied_at = self._clock()
        in_scope = list(self._schedules.schedules_for_scope(workflow_name, store_code))
        enabled_before = [schedule.id for schedule in in_scope if schedule.enabled]
        already_disabled = tuple(schedule.id for schedule in in_scope if not schedule.enabled)
        for schedule_id in enabled_before:
            self._schedules.set_schedule_enabled(
                schedule_id, enabled=False, actor=principal.identity, reason=reason
            )

        self._audit.append_audit_entry(
            actor=principal.identity,
            action=FALLBACK_APPLIED,
            reason=reason,
            resource_type=FALLBACK_RESOURCE,
            resource_key=resource_key,
            outcome="APPLIED",
            before_evidence={
                "enabled_schedule_ids": [str(schedule_id) for schedule_id in enabled_before],
            },
            after_evidence={
                "enabled_schedule_ids": [],
                "already_disabled_schedule_ids": [str(s) for s in already_disabled],
                "reconcile_window_from": applied_at.isoformat(),
            },
        )
        return FallbackOutcome(
            disabled_schedule_ids=tuple(enabled_before),
            already_disabled=already_disabled,
            applied_at=applied_at,
        )

    # -- helpers -------------------------------------------------------------

    def _set_schedule(
        self, principal: Principal, schedule_id: UUID, reason: str, *, enabled: bool
    ) -> object:
        _require_reason(reason)
        operation = Operation.SCHEDULE_ENABLE if enabled else Operation.SCHEDULE_DISABLE
        self._authorize(principal, operation, str(schedule_id))
        return self._schedules.set_schedule_enabled(
            schedule_id, enabled=enabled, actor=principal.identity, reason=reason
        )

    @staticmethod
    def _control[T: (RetryRequest, ReplayRequest, SnapshotReplayRequest)](kind: type[T], **fields: object) -> T:
        """Build a domain control request, translating its refusal."""

        try:
            return kind(**fields)  # type: ignore[arg-type]
        except InvalidWorkflowControl as error:
            raise InvalidOperationRequest(str(error)) from None
