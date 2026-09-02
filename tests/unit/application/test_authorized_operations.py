"""The authorized manual-operations service (FR-023, #26).

Every listed operation -- trigger, status, retry, replay, schedule
enable/disable, reconciliation, fallback -- passes through one service that
checks the principal's role, records a refusal in the audit ledger, and only
then delegates to the persistence port. The ports are the existing
repositories' method shapes; here they are fakes, so the tests prove the
service's own behaviour and nothing about SQL.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from esl_service.application.operations import (
    AuthorizedOperations,
    FallbackOutcome,
    InvalidOperationRequest,
)
from esl_service.domain.authorization import (
    AUTHORIZATION_POLICY_VERSION,
    AUTHORIZATION_RESOURCE,
    FALLBACK_APPLIED,
    FALLBACK_RESOURCE,
    OPERATION_REFUSED,
    RECONCILIATION_REQUESTED,
    NotAuthorized,
    Operation,
    Principal,
    Role,
)
from esl_service.domain.operations import ExecutionQuery, ReplayRequest, RetryRequest
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.scheduling import ManualLaunch

OPERATOR = Principal("alice", frozenset({Role.OPERATOR}))
ADMIN = Principal("root", frozenset({Role.ADMIN}))
NOBODY = Principal("guest", frozenset())

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 2, 7, 30, tzinfo=UTC)


# --- fakes shaped like the repositories --------------------------------------


@dataclass
class FakeSchedule:
    id: UUID
    workflow_name: str
    store_code: str
    enabled: bool
    configuration_version_id: UUID = field(default_factory=uuid4)


@dataclass
class FakeAudit:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append_audit_entry(self, **fields: Any) -> dict[str, Any]:
        self.entries.append(fields)
        return fields

    def actions(self) -> list[str]:
        return [entry["action"] for entry in self.entries]


@dataclass
class FakeLaunches:
    manual: list[dict[str, Any]] = field(default_factory=list)
    retries: list[tuple[UUID, RetryRequest]] = field(default_factory=list)
    replays: list[tuple[UUID, ReplayRequest]] = field(default_factory=list)
    schedules: list[FakeSchedule] = field(default_factory=list)
    toggled: list[tuple[UUID, bool, str, str]] = field(default_factory=list)

    def launch_manual(self, launch: ManualLaunch, **fields: Any) -> str:
        self.manual.append({"launch": launch, **fields})
        return "launched"

    def launch_retry(self, execution_id: UUID, request: RetryRequest, **_: Any) -> str:
        self.retries.append((execution_id, request))
        return "retried"

    def launch_replay(self, execution_id: UUID, request: ReplayRequest, **_: Any) -> str:
        self.replays.append((execution_id, request))
        return "replayed"

    def schedules_for_scope(
        self, workflow_name: str, store_code: str | None
    ) -> Sequence[FakeSchedule]:
        return [
            s
            for s in self.schedules
            if s.workflow_name == workflow_name
            and (store_code is None or s.store_code == store_code)
        ]

    def set_schedule_enabled(
        self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str
    ) -> FakeSchedule:
        schedule = next(s for s in self.schedules if s.id == schedule_id)
        schedule.enabled = enabled
        self.toggled.append((schedule_id, enabled, actor, reason))
        return schedule


@dataclass
class FakeStatus:
    queries: list[ExecutionQuery] = field(default_factory=list)

    def query_executions(self, query: ExecutionQuery) -> Sequence[str]:
        self.queries.append(query)
        return ["execution"]


@dataclass
class FakeReconciliation:
    finalized: list[tuple[UUID, ReconciliationMode, ReconciliationCounts]] = field(
        default_factory=list
    )

    def finalize_report(
        self, execution_id: UUID, mode: ReconciliationMode, counts: ReconciliationCounts
    ) -> str:
        self.finalized.append((execution_id, mode, counts))
        return "report"


@dataclass
class Harness:
    launches: FakeLaunches
    status: FakeStatus
    reconciliation: FakeReconciliation
    audit: FakeAudit
    service: AuthorizedOperations


@pytest.fixture
def harness() -> Harness:
    launches, status, reconciliation, audit = (
        FakeLaunches(),
        FakeStatus(),
        FakeReconciliation(),
        FakeAudit(),
    )
    service = AuthorizedOperations(
        launches=launches,
        schedules=launches,
        status=status,
        reconciliation=reconciliation,
        audit=audit,
        clock=lambda: NOW,
    )
    return Harness(launches, status, reconciliation, audit, service)


def trigger(service: AuthorizedOperations, principal: Principal, reason: str = "CHG-1") -> Any:
    return service.trigger(
        principal,
        reason,
        workflow_name="esl-refresh",
        store_code="084",
        mode=ExecutionMode.SHADOW,
        correlation_id=uuid4(),
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
        configuration_version_id=uuid4(),
        rule_version="compatibility-v1",
    )


def counts() -> ReconciliationCounts:
    return ReconciliationCounts(
        extracted=0,
        rejected=0,
        valid=0,
        ineligible=0,
        eligible=0,
        unchanged=0,
        skipped_idempotent=0,
        intended=0,
        acknowledged=0,
        rejected_by_aims=0,
        failed=0,
        unresolved=0,
        submitted=0,
        ambiguous=0,
    )


# --- every operation is role-checked (acceptance criterion 1) -----------------


def test_an_operator_trigger_delegates_with_identity_and_reason(harness: Harness) -> None:
    result = trigger(harness.service, OPERATOR, "CHG-1")

    assert result == "launched"
    launch = harness.launches.manual[0]["launch"]
    assert launch.requested_by == "alice"
    assert launch.reason == "CHG-1"
    assert harness.audit.entries == []  # the launch itself is audited by the repository


def test_an_unauthorized_trigger_is_refused_audited_and_never_delegated(
    harness: Harness,
) -> None:
    with pytest.raises(NotAuthorized):
        trigger(harness.service, NOBODY)

    assert harness.launches.manual == []
    (entry,) = harness.audit.entries
    assert entry["action"] == OPERATION_REFUSED
    assert entry["actor"] == "guest"
    assert entry["resource_type"] == AUTHORIZATION_RESOURCE
    assert entry["outcome"] == "REFUSED"
    assert entry["after_evidence"]["operation"] == Operation.TRIGGER.value
    assert entry["after_evidence"]["required_role"] == Role.OPERATOR.value
    assert entry["after_evidence"]["policy_version"] == AUTHORIZATION_POLICY_VERSION


def test_status_is_an_operator_read_and_needs_no_reason(harness: Harness) -> None:
    query = ExecutionQuery(workflow_name="esl-refresh")

    result = harness.service.status(OPERATOR, query)

    assert result == ["execution"]
    assert harness.status.queries == [query]


def test_status_is_refused_without_a_role(harness: Harness) -> None:
    with pytest.raises(NotAuthorized):
        harness.service.status(NOBODY, ExecutionQuery(workflow_name="esl-refresh"))

    assert harness.status.queries == []
    assert harness.audit.actions() == [OPERATION_REFUSED]


def test_retry_carries_identity_and_reason_into_the_request(harness: Harness) -> None:
    execution_id = uuid4()

    harness.service.retry(OPERATOR, execution_id, "CHG-2", correlation_id=uuid4())

    ((retried_id, request),) = harness.launches.retries
    assert retried_id == execution_id
    assert request.requested_by == "alice"
    assert request.reason == "CHG-2"


def test_replay_requires_both_window_bounds(harness: Harness) -> None:
    with pytest.raises(InvalidOperationRequest):
        harness.service.replay(
            OPERATOR,
            uuid4(),
            "CHG-3",
            correlation_id=uuid4(),
            source_window_start=WINDOW_START,
            source_window_end=None,
        )

    assert harness.launches.replays == []


def test_replay_delegates_one_validated_window(harness: Harness) -> None:
    harness.service.replay(
        OPERATOR,
        uuid4(),
        "CHG-3",
        correlation_id=uuid4(),
        source_window_start=WINDOW_START,
        source_window_end=WINDOW_END,
    )

    ((_, request),) = harness.launches.replays
    assert (request.source_window_start, request.source_window_end) == (
        WINDOW_START,
        WINDOW_END,
    )


def test_schedule_disable_is_admin_only(harness: Harness) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.launches.schedules.append(schedule)

    with pytest.raises(NotAuthorized):
        harness.service.disable_schedule(OPERATOR, schedule.id, "CHG-4")

    assert schedule.enabled is True
    assert harness.audit.actions() == [OPERATION_REFUSED]


def test_an_admin_toggles_a_schedule_under_their_own_identity(harness: Harness) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.launches.schedules.append(schedule)

    harness.service.disable_schedule(ADMIN, schedule.id, "CHG-4")
    harness.service.enable_schedule(ADMIN, schedule.id, "CHG-5")

    assert harness.launches.toggled == [
        (schedule.id, False, "root", "CHG-4"),
        (schedule.id, True, "root", "CHG-5"),
    ]


def test_reconcile_is_audited_as_requested_by_the_operator(harness: Harness) -> None:
    execution_id = uuid4()

    harness.service.reconcile(
        OPERATOR, execution_id, "CHG-6", mode=ReconciliationMode.SHADOW, counts=counts()
    )

    assert harness.reconciliation.finalized[0][0] == execution_id
    (entry,) = harness.audit.entries
    assert entry["action"] == RECONCILIATION_REQUESTED
    assert entry["actor"] == "alice"
    assert entry["reason"] == "CHG-6"
    assert entry["execution_id"] == execution_id


# --- a reason is mandatory for every mutation (acceptance criterion 2) --------


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_is_refused_before_any_delegation(
    harness: Harness, reason: str
) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.launches.schedules.append(schedule)

    with pytest.raises(InvalidOperationRequest):
        trigger(harness.service, OPERATOR, reason)
    with pytest.raises(InvalidOperationRequest):
        harness.service.retry(OPERATOR, uuid4(), reason, correlation_id=uuid4())
    with pytest.raises(InvalidOperationRequest):
        harness.service.disable_schedule(ADMIN, schedule.id, reason)
    with pytest.raises(InvalidOperationRequest):
        harness.service.fallback(ADMIN, reason, workflow_name="esl-refresh")

    assert harness.launches.manual == []
    assert harness.launches.retries == []
    assert harness.launches.toggled == []
    assert harness.audit.entries == []


# --- fallback (acceptance criterion 3) ---------------------------------------


def test_fallback_disables_every_enabled_schedule_in_scope_and_audits_once(
    harness: Harness,
) -> None:
    """The in-application part of the cutover rollback (SPECIFICATION 8)."""

    on_084 = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    on_075 = FakeSchedule(uuid4(), "esl-refresh", "075", enabled=True)
    already_off = FakeSchedule(uuid4(), "esl-refresh", "090", enabled=False)
    other_workflow = FakeSchedule(uuid4(), "sku-master", "084", enabled=True)
    harness.launches.schedules.extend([on_084, on_075, already_off, other_workflow])

    outcome = harness.service.fallback(ADMIN, "INC-9", workflow_name="esl-refresh")

    assert isinstance(outcome, FallbackOutcome)
    assert set(outcome.disabled_schedule_ids) == {on_084.id, on_075.id}
    assert outcome.already_disabled == (already_off.id,)
    assert outcome.applied_at == NOW
    assert other_workflow.enabled is True
    assert {s.id for s in harness.launches.schedules if not s.enabled} == {
        on_084.id,
        on_075.id,
        already_off.id,
    }

    fallback_entries = [e for e in harness.audit.entries if e["action"] == FALLBACK_APPLIED]
    (entry,) = fallback_entries
    assert entry["actor"] == "root"
    assert entry["reason"] == "INC-9"
    assert entry["resource_type"] == FALLBACK_RESOURCE
    assert entry["resource_key"] == "esl-refresh"
    assert entry["outcome"] == "APPLIED"
    assert set(entry["before_evidence"]["enabled_schedule_ids"]) == {
        str(on_084.id),
        str(on_075.id),
    }
    assert entry["after_evidence"]["enabled_schedule_ids"] == []
    assert entry["after_evidence"]["reconcile_window_from"] == NOW.isoformat()


def test_fallback_can_be_bounded_to_one_store(harness: Harness) -> None:
    on_084 = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    on_075 = FakeSchedule(uuid4(), "esl-refresh", "075", enabled=True)
    harness.launches.schedules.extend([on_084, on_075])

    outcome = harness.service.fallback(
        ADMIN, "INC-9", workflow_name="esl-refresh", store_code="084"
    )

    assert outcome.disabled_schedule_ids == (on_084.id,)
    assert on_075.enabled is True
    entry = next(e for e in harness.audit.entries if e["action"] == FALLBACK_APPLIED)
    assert entry["resource_key"] == "esl-refresh/084"


def test_fallback_with_nothing_to_disable_is_still_recorded(harness: Harness) -> None:
    """An operator must be able to see that fallback ran, even if it was a no-op."""

    outcome = harness.service.fallback(ADMIN, "INC-9", workflow_name="esl-refresh")

    assert outcome.disabled_schedule_ids == ()
    assert harness.audit.actions() == [FALLBACK_APPLIED]


def test_fallback_is_refused_for_an_operator_and_changes_nothing(harness: Harness) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.launches.schedules.append(schedule)

    with pytest.raises(NotAuthorized):
        harness.service.fallback(OPERATOR, "INC-9", workflow_name="esl-refresh")

    assert schedule.enabled is True
    assert harness.audit.actions() == [OPERATION_REFUSED]


def test_fallback_never_deletes_or_touches_executions(harness: Harness) -> None:
    """Rollback preserves target state and audit (SPECIFICATION 8); only schedules change."""

    harness.service.fallback(ADMIN, "INC-9", workflow_name="esl-refresh")

    assert harness.launches.manual == []
    assert harness.launches.retries == []
    assert harness.launches.replays == []
    assert harness.reconciliation.finalized == []
