"""Operator commands on the command line (FR-029, #28).

The CLI is the second surface over the same #26 service the API uses: the
principal is the running Windows account under ``ESL_OPERATOR_ROLES``, the
reason is mandatory, and a role refusal is exit code 3 after the refusal has
been audited. The commands are exercised through Typer's runner with the
service, principal, and health report injected through module seams, so no
test touches a database or the Windows API.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from esl_service.application.operations import AuthorizedOperations
from esl_service.domain.authorization import OPERATION_REFUSED, Principal, Role
from esl_service.domain.operations import ExecutionQuery, ReplayRequest, RetryRequest
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import ManualLaunch
from esl_service.runtime import cli, cli_operations
from esl_service.runtime.health import DependencyHealth, HealthService, HealthState
from esl_service.runtime.scheduler import LaunchContext

runner = CliRunner()
NOW = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


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
    configuration_version_id: UUID = field(default_factory=uuid4)
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
        return FakeLaunchResult(FakeExecution(uuid4(), trigger_type="RETRY"))

    def launch_replay(
        self, execution_id: UUID, request: ReplayRequest, **_: Any
    ) -> FakeLaunchResult:
        self.replays.append((execution_id, request))
        return FakeLaunchResult(FakeExecution(uuid4(), trigger_type="REPLAY"))

    def schedules_for_scope(self, workflow_name: str, store_code: str | None) -> Sequence[FakeSchedule]:
        return [s for s in self.schedules if s.workflow_name == workflow_name]

    def set_schedule_enabled(self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str) -> FakeSchedule:
        schedule = next(s for s in self.schedules if s.id == schedule_id)
        schedule.enabled = enabled
        return schedule

    def query_executions(self, query: ExecutionQuery) -> Sequence[FakeExecution]:
        return [
            e
            for e in self.executions
            if (query.execution_id is None or e.id == query.execution_id)
            and (query.store_code is None or e.store_code == query.store_code)
        ]

    def finalize_report(self, execution_id: UUID, mode: object, counts: object) -> object:
        return object()

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
        return DependencyHealth(name=self.name, state=HealthState.UNAVAILABLE, required=True, detail="refused")


OPERATOR = Principal("budi", frozenset({Role.OPERATOR}))
ADMIN = Principal("pepito", frozenset({Role.ADMIN}))
GUEST = Principal("guest", frozenset())


@pytest.fixture
def repositories(monkeypatch: pytest.MonkeyPatch) -> FakeRepositories:
    repositories = FakeRepositories()
    operations = AuthorizedOperations(
        launches=repositories,
        schedules=repositories,
        status=repositories,
        reconciliation=repositories,
        audit=repositories,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(cli_operations, "_operations", lambda: operations)
    monkeypatch.setattr(cli_operations, "_principal", lambda: OPERATOR)
    monkeypatch.setattr(cli_operations, "_health", lambda: HealthService([HealthyProbe()]))
    monkeypatch.setattr(
        cli_operations,
        "_context",
        lambda: LaunchContext(ExecutionMode.SHADOW, uuid4(), "compatibility-v1"),
    )
    return repositories


def as_principal(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(cli_operations, "_principal", lambda: principal)


# --- status ---------------------------------------------------------------------


def test_status_prints_readiness_and_each_dependency(repositories: FakeRepositories) -> None:
    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "ready" in result.output.lower()
    assert "state_store" in result.output
    assert "HEALTHY" in result.output


def test_status_exits_nonzero_when_not_ready(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_operations, "_health", lambda: HealthService([DownProbe()]))

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "UNAVAILABLE" in result.output


# --- runs -------------------------------------------------------------------------


START = [
    "runs", "start", "--workflow", "esl-refresh", "--store", "084",
    "--reason", "CHG-1 manual refresh",
    "--window-start", "2026-09-02T07:00:00+00:00", "--window-end", "2026-09-02T07:30:00+00:00",
]


def test_runs_start_launches_under_the_running_account(repositories: FakeRepositories) -> None:
    result = runner.invoke(cli.app, START)

    assert result.exit_code == 0, result.output
    (execution,) = repositories.executions
    assert execution.requested_by == "budi"
    assert execution.reason == "CHG-1 manual refresh"
    assert str(execution.id) in result.output


def test_runs_start_is_refused_for_an_account_without_a_role(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_principal(monkeypatch, GUEST)

    result = runner.invoke(cli.app, START)

    assert result.exit_code == cli_operations.EXIT_NOT_AUTHORIZED
    assert "guest" in result.output and "operator" in result.output
    assert repositories.executions == []
    assert repositories.audit[-1]["action"] == OPERATION_REFUSED


def test_runs_start_requires_a_reason(repositories: FakeRepositories) -> None:
    result = runner.invoke(cli.app, [a for a in START if a not in ("--reason", "CHG-1 manual refresh")])

    assert result.exit_code != 0
    assert repositories.executions == []


def test_runs_start_rejects_a_naive_window(repositories: FakeRepositories) -> None:
    args = [*START]
    args[args.index("--window-start") + 1] = "2026-09-02T07:00:00"

    result = runner.invoke(cli.app, args)

    assert result.exit_code != 0
    assert "timezone" in result.output.lower()
    assert repositories.executions == []


def test_runs_show_prints_one_run_and_exits_one_when_absent(
    repositories: FakeRepositories,
) -> None:
    runner.invoke(cli.app, START)
    (execution,) = repositories.executions

    shown = runner.invoke(cli.app, ["runs", "show", str(execution.id)])
    missing = runner.invoke(cli.app, ["runs", "show", str(uuid4())])

    assert shown.exit_code == 0 and "QUEUED" in shown.output and "084" in shown.output
    assert missing.exit_code == 1


def test_runs_list_filters_by_store(repositories: FakeRepositories) -> None:
    runner.invoke(cli.app, START)
    other = [*START]
    other[other.index("--store") + 1] = "075"
    runner.invoke(cli.app, other)

    result = runner.invoke(cli.app, ["runs", "list", "--store", "084"])

    assert result.exit_code == 0, result.output
    assert "084" in result.output and "075" not in result.output


def test_runs_retry_and_replay_carry_identity_and_reason(repositories: FakeRepositories) -> None:
    origin = uuid4()

    retry = runner.invoke(cli.app, ["runs", "retry", str(origin), "--reason", "CHG-2"])
    replay = runner.invoke(
        cli.app,
        ["runs", "replay", str(origin), "--reason", "CHG-3",
         "--window-start", "2026-09-02T07:00:00+00:00", "--window-end", "2026-09-02T07:30:00+00:00"],
    )

    assert retry.exit_code == 0, retry.output
    assert replay.exit_code == 0, replay.output
    assert repositories.retries[0][1].requested_by == "budi"
    assert repositories.replays[0][1].reason == "CHG-3"


# --- schedules and fallback -----------------------------------------------------------


def test_schedules_disable_is_admin_only(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    repositories.schedules.append(schedule)

    refused = runner.invoke(cli.app, ["schedules", "disable", str(schedule.id), "--reason", "CHG-4"])
    as_principal(monkeypatch, ADMIN)
    applied = runner.invoke(cli.app, ["schedules", "disable", str(schedule.id), "--reason", "CHG-4"])
    restored = runner.invoke(cli.app, ["schedules", "enable", str(schedule.id), "--reason", "CHG-5"])

    assert refused.exit_code == cli_operations.EXIT_NOT_AUTHORIZED
    assert applied.exit_code == 0, applied.output
    assert "disabled" in applied.output.lower()
    assert restored.exit_code == 0, restored.output
    assert schedule.enabled is True


def test_fallback_reports_what_it_disabled(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = FakeSchedule(uuid4(), "esl-refresh", "084", enabled=True)
    repositories.schedules.append(on)
    as_principal(monkeypatch, ADMIN)

    result = runner.invoke(cli.app, ["fallback", "--workflow", "esl-refresh", "--reason", "INC-9"])

    assert result.exit_code == 0, result.output
    assert str(on.id) in result.output
    assert on.enabled is False
    assert repositories.audit[-1]["action"] == "fallback.applied"


def test_fallback_is_refused_for_an_operator(repositories: FakeRepositories) -> None:
    result = runner.invoke(cli.app, ["fallback", "--workflow", "esl-refresh", "--reason", "INC-9"])

    assert result.exit_code == cli_operations.EXIT_NOT_AUTHORIZED


# --- the seams fail closed -------------------------------------------------------------


def test_a_service_that_cannot_be_built_is_reported_without_a_traceback(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> AuthorizedOperations:
        raise cli_operations.OperationsUnavailable("state store is unreachable")

    monkeypatch.setattr(cli_operations, "_operations", broken)

    result = runner.invoke(cli.app, START)

    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert "Traceback" not in result.output
