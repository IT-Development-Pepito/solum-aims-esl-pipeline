"""The internal operations API (FR-029, #28).

Every mutating route is the FR-023 operation of the same name, reached
through ``AuthorizedOperations`` (#26), so the API adds authentication and
transport only: a missing or unknown token is 401, a role refusal is 403 and
is already in the audit ledger by the time the response is built, and a
malformed request is 422 before any role check. Health routes need no token,
because a monitor must be able to ask whether the process is alive without
holding an operator credential.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from esl_service.application.operations import AuthorizedOperations
from esl_service.domain.authorization import OPERATION_REFUSED, Role
from esl_service.domain.operations import ExecutionQuery, ReplayRequest, RetryRequest
from esl_service.domain.scheduling import ManualLaunch
from esl_service.runtime.health import DependencyHealth, HealthService, HealthState
from esl_service.runtime.scheduler import Scheduler
from esl_service.web.app import create_app
from esl_service.web.auth import BearerTokenAuthenticator

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
CONFIGURATION_VERSION_ID = uuid4()


# --- fakes ---------------------------------------------------------------------


@dataclass
class FakeExecution:
    id: UUID
    workflow_name: str = "esl-refresh"
    store_code: str = "084"
    trigger_type: str = "MANUAL"
    mode: str = "SHADOW"
    correlation_id: UUID = field(default_factory=uuid4)
    source_window_start: datetime = NOW
    source_window_end: datetime = NOW
    configuration_version_id: UUID = CONFIGURATION_VERSION_ID
    rule_version: str = "compatibility-v1"
    requested_by: str | None = "pepito"
    reason: str | None = "CHG-1"
    retry_of_execution_id: UUID | None = None
    replay_of_execution_id: UUID | None = None
    started_at: datetime = NOW
    ended_at: datetime | None = None
    status: str = "QUEUED"
    terminal_reason: str | None = None


@dataclass(frozen=True)
class FakeLaunchResult:
    execution: FakeExecution | None
    schedule_refusal: object | None = None
    ownership: object | None = None
    control_refusal: object | None = None

    @property
    def launched(self) -> bool:
        return self.execution is not None


@dataclass
class FakeSchedule:
    id: UUID
    workflow_name: str
    store_code: str
    enabled: bool


@dataclass
class FakeRepositories:
    executions: list[FakeExecution] = field(default_factory=list)
    schedules: list[FakeSchedule] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    retries: list[tuple[UUID, RetryRequest]] = field(default_factory=list)
    replays: list[tuple[UUID, ReplayRequest]] = field(default_factory=list)

    # LaunchPort / SchedulePort
    def launch_manual(self, launch: ManualLaunch, **fields: Any) -> FakeLaunchResult:
        execution = FakeExecution(
            uuid4(),
            workflow_name=fields["workflow_name"],
            store_code=fields["store_code"],
            requested_by=launch.requested_by,
            reason=launch.reason,
        )
        self.executions.append(execution)
        return FakeLaunchResult(execution)

    def launch_retry(self, execution_id: UUID, request: RetryRequest, **_: Any) -> FakeLaunchResult:
        self.retries.append((execution_id, request))
        execution = FakeExecution(uuid4(), trigger_type="RETRY", retry_of_execution_id=execution_id)
        self.executions.append(execution)
        return FakeLaunchResult(execution)

    def launch_replay(
        self, execution_id: UUID, request: ReplayRequest, **_: Any
    ) -> FakeLaunchResult:
        self.replays.append((execution_id, request))
        execution = FakeExecution(uuid4(), trigger_type="REPLAY", replay_of_execution_id=execution_id)
        self.executions.append(execution)
        return FakeLaunchResult(execution)

    def schedules_for_scope(self, workflow_name: str, store_code: str | None) -> Sequence[FakeSchedule]:
        return [
            s
            for s in self.schedules
            if s.workflow_name == workflow_name and (store_code is None or s.store_code == store_code)
        ]

    def set_schedule_enabled(self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str) -> FakeSchedule:
        schedule = next((s for s in self.schedules if s.id == schedule_id), None)
        if schedule is None:
            raise LookupError(f"no schedule with id {schedule_id}")
        schedule.enabled = enabled
        return schedule

    # StatusPort
    def query_executions(self, query: ExecutionQuery) -> Sequence[FakeExecution]:
        return [
            e
            for e in self.executions
            if (query.execution_id is None or e.id == query.execution_id)
            and (query.store_code is None or e.store_code == query.store_code)
            and (query.workflow_name is None or e.workflow_name == query.workflow_name)
        ]

    # ReconciliationPort
    def finalize_report(self, execution_id: UUID, mode: object, counts: object) -> object:
        return object()

    # AuditPort
    def append_audit_entry(self, **fields: Any) -> dict[str, Any]:
        self.audit.append(fields)
        return fields


class HealthyProbe:
    name = "state_store"
    required = True

    def check(self) -> DependencyHealth:
        return DependencyHealth(name=self.name, state=HealthState.HEALTHY, required=True, detail=None)


class DownProbe(HealthyProbe):
    def check(self) -> DependencyHealth:
        return DependencyHealth(
            name=self.name, state=HealthState.UNAVAILABLE, required=True, detail="refused"
        )


class NoLaunches:
    def due_schedules(self, instant: datetime) -> list[object]:
        return []

    def launch_scheduled(self, schedule_id: UUID, **fields: Any) -> object:
        raise AssertionError("not expected")


@dataclass
class Harness:
    repositories: FakeRepositories
    scheduler: Scheduler
    client: TestClient


def build(probe: HealthyProbe | None = None) -> Harness:
    repositories = FakeRepositories()
    operations = AuthorizedOperations(
        launches=repositories,
        schedules=repositories,
        status=repositories,
        reconciliation=repositories,
        audit=repositories,
        clock=lambda: NOW,
    )
    from esl_service.domain.outcomes import ExecutionMode
    from esl_service.runtime.scheduler import LaunchContext

    scheduler = Scheduler(
        NoLaunches(),
        LaunchContext(ExecutionMode.SHADOW, CONFIGURATION_VERSION_ID, "compatibility-v1"),
    )
    authenticator = BearerTokenAuthenticator(
        tokens={"pepito": "tok-admin", "budi": "tok-op", "guest": "tok-guest"},
        assignments={"pepito": frozenset({Role.ADMIN}), "budi": frozenset({Role.OPERATOR})},
    )
    app = create_app(
        operations=operations,
        authenticator=authenticator,
        health=HealthService([probe or HealthyProbe()]),
        scheduler=scheduler,
        audit=repositories,
        configuration_version_id=CONFIGURATION_VERSION_ID,
        clock=lambda: NOW,
    )
    return Harness(repositories, scheduler, TestClient(app))


@pytest.fixture
def harness() -> Harness:
    return build()


ADMIN = {"Authorization": "Bearer tok-admin"}
OPERATOR = {"Authorization": "Bearer tok-op"}
GUEST = {"Authorization": "Bearer tok-guest"}

RUN_BODY = {
    "workflow_name": "esl-refresh",
    "store_code": "084",
    "reason": "CHG-1 manual refresh",
    "source_window_start": "2026-09-02T07:00:00+00:00",
    "source_window_end": "2026-09-02T07:30:00+00:00",
}


# --- health needs no token ----------------------------------------------------


def test_liveness_is_open_and_always_alive(harness: Harness) -> None:
    response = harness.client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"alive": True}


def test_readiness_reports_dependencies_and_is_503_when_not_ready() -> None:
    harness = build(DownProbe())

    response = harness.client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["dependencies"][0]["name"] == "state_store"
    assert body["dependencies"][0]["state"] == "UNAVAILABLE"


def test_readiness_is_200_when_ready(harness: Harness) -> None:
    response = harness.client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True


# --- authentication ---------------------------------------------------------------


def test_a_request_without_a_token_is_401(harness: Harness) -> None:
    response = harness.client.post("/runs", json=RUN_BODY)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert harness.repositories.executions == []


def test_an_unknown_token_is_401_and_never_echoed(harness: Harness) -> None:
    response = harness.client.post(
        "/runs", json=RUN_BODY, headers={"Authorization": "Bearer stolen-needle"}
    )

    assert response.status_code == 401
    assert "stolen-needle" not in response.text


# --- authorization is the #26 service ---------------------------------------------


def test_a_role_refusal_is_403_and_already_audited(harness: Harness) -> None:
    response = harness.client.post("/runs", json=RUN_BODY, headers=GUEST)

    assert response.status_code == 403
    body = response.json()
    assert body["operation"] == "trigger"
    assert body["required_role"] == "operator"
    assert harness.repositories.audit[-1]["action"] == OPERATION_REFUSED
    assert harness.repositories.audit[-1]["actor"] == "guest"
    assert harness.repositories.executions == []


# --- operations ---------------------------------------------------------------------


def test_trigger_creates_a_run_under_the_token_holder(harness: Harness) -> None:
    response = harness.client.post("/runs", json=RUN_BODY, headers=OPERATOR)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["launched"] is True
    assert body["execution"]["requested_by"] == "budi"
    assert body["execution"]["status"] == "QUEUED"
    UUID(body["execution"]["id"])


def test_trigger_with_a_blank_reason_is_422_before_any_role_check(harness: Harness) -> None:
    response = harness.client.post("/runs", json={**RUN_BODY, "reason": " "}, headers=GUEST)

    assert response.status_code == 422
    assert harness.repositories.audit == []


def test_trigger_with_a_reversed_window_is_422(harness: Harness) -> None:
    body = {
        **RUN_BODY,
        "source_window_start": RUN_BODY["source_window_end"],
        "source_window_end": RUN_BODY["source_window_start"],
    }

    assert harness.client.post("/runs", json=body, headers=OPERATOR).status_code == 422


def test_status_lists_runs_by_store(harness: Harness) -> None:
    harness.client.post("/runs", json=RUN_BODY, headers=OPERATOR)
    harness.client.post("/runs", json={**RUN_BODY, "store_code": "075"}, headers=OPERATOR)

    response = harness.client.get("/runs", params={"store_code": "084"}, headers=OPERATOR)

    assert response.status_code == 200
    assert [r["store_code"] for r in response.json()] == ["084"]


def test_status_without_any_selector_is_422(harness: Harness) -> None:
    assert harness.client.get("/runs", headers=OPERATOR).status_code == 422


def test_one_run_is_fetched_by_id_and_404_when_absent(harness: Harness) -> None:
    created = harness.client.post("/runs", json=RUN_BODY, headers=OPERATOR).json()["execution"]

    found = harness.client.get(f"/runs/{created['id']}", headers=OPERATOR)
    missing = harness.client.get(f"/runs/{uuid4()}", headers=OPERATOR)

    assert found.status_code == 200 and found.json()["id"] == created["id"]
    assert missing.status_code == 404


def test_retry_and_replay_carry_the_token_holder_and_reason(harness: Harness) -> None:
    origin = uuid4()

    retry = harness.client.post(
        f"/runs/{origin}/retry", json={"reason": "CHG-2"}, headers=OPERATOR
    )
    replay = harness.client.post(
        f"/runs/{origin}/replay",
        json={
            "reason": "CHG-3",
            "source_window_start": RUN_BODY["source_window_start"],
            "source_window_end": RUN_BODY["source_window_end"],
        },
        headers=OPERATOR,
    )

    assert retry.status_code == 202 and replay.status_code == 202
    ((_, retry_request),) = harness.repositories.retries
    ((_, replay_request),) = harness.repositories.replays
    assert (retry_request.requested_by, retry_request.reason) == ("budi", "CHG-2")
    assert replay_request.requested_by == "budi"


def test_schedule_enable_and_disable_are_admin_only(harness: Harness) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.repositories.schedules.append(schedule)

    refused = harness.client.post(
        f"/schedules/{schedule.id}/disable", json={"reason": "CHG-4"}, headers=OPERATOR
    )
    applied = harness.client.post(
        f"/schedules/{schedule.id}/disable", json={"reason": "CHG-4"}, headers=ADMIN
    )
    restored = harness.client.post(
        f"/schedules/{schedule.id}/enable", json={"reason": "CHG-5"}, headers=ADMIN
    )

    assert refused.status_code == 403
    assert applied.status_code == 200 and applied.json()["enabled"] is False
    assert restored.status_code == 200 and restored.json()["enabled"] is True


def test_an_unknown_schedule_is_404(harness: Harness) -> None:
    response = harness.client.post(
        f"/schedules/{uuid4()}/disable", json={"reason": "CHG-4"}, headers=ADMIN
    )

    assert response.status_code == 404


def test_fallback_disables_the_scope_and_reports_what_it_did(harness: Harness) -> None:
    on = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    harness.repositories.schedules.append(on)

    response = harness.client.post(
        "/fallback", json={"workflow_name": "esl-refresh", "reason": "INC-9"}, headers=ADMIN
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["disabled_schedule_ids"] == [str(on.id)]
    assert datetime.fromisoformat(body["applied_at"]) == NOW
    assert on.enabled is False


# --- scheduler pause and resume (service lifecycle over the API) --------------------


def test_scheduler_pause_and_resume_are_admin_operations_and_audited(harness: Harness) -> None:
    paused = harness.client.post("/scheduler/pause", json={"reason": "INC-1"}, headers=ADMIN)
    state = harness.client.get("/scheduler", headers=OPERATOR)
    resumed = harness.client.post("/scheduler/resume", json={"reason": "INC-1"}, headers=ADMIN)

    assert paused.status_code == 200 and paused.json()["paused"] is True
    assert state.json()["paused"] is True
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    actions = [entry["action"] for entry in harness.repositories.audit]
    assert actions == ["scheduler.paused", "scheduler.resumed"]
    assert harness.repositories.audit[0]["actor"] == "pepito"


def test_an_operator_may_not_pause_the_scheduler(harness: Harness) -> None:
    response = harness.client.post("/scheduler/pause", json={"reason": "x"}, headers=OPERATOR)

    assert response.status_code == 403
    assert harness.scheduler.paused is False


# --- nothing leaks -------------------------------------------------------------------


def test_the_openapi_document_is_served_only_to_authenticated_callers(harness: Harness) -> None:
    assert harness.client.get("/openapi.json").status_code == 401
    assert harness.client.get("/openapi.json", headers=OPERATOR).status_code == 200
