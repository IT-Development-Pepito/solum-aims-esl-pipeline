"""FR-016 failure and recovery scenarios (#21).

One focused test per named event. Every scenario runs against committed
rows in the dedicated test database, because recovery is by definition what
a *different* process or engine finds in durable state; the rolled-back
fixtures elsewhere cannot show that. Each test purges what it committed.

Fidelity, as decided by the owner on 2026-09-03:

* SQL Server and malformed-data faults are injected as the signal the
  adapters raise (the production SQL Server sources are never touched);
* AIMS faults are real: a TCP forwarder in front of the local AIMS clone
  refuses or cuts the connection and the real reader classifies it;
* application restart is a real process death after a named checkpoint;
* server restart is simulated by a fresh engine over the same state.

The AIMS/API mutation event is #113, blocked by #23.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from esl_service.adapters.aims_compatibility import (
    AimsCompatibilityReader,
    AimsUnavailable,
    create_read_only_engine,
)
from esl_service.application.contracts import SourceWindow
from esl_service.application.recovery import report_for
from esl_service.application.runner import (
    EVENT_RUN_RECOVERY_MARKED,
    EVENT_STEP_RESUMED,
    STEP_CANONICALIZE,
    STEP_DISCOVER,
    STEP_PERSIST,
    STEP_READ_STORE,
    STEP_READ_WAREHOUSE,
    WorkflowRunner,
)
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    classify,
)
from esl_service.domain.outcomes import ExecutionMode, FailureClass
from esl_service.domain.scheduling import ManualLaunch
from esl_service.domain.workflow import ExecutionStatus
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.models import (
    ExecutionEvent,
    ExecutionStep,
    ReconciliationReport,
    RecordAction,
    SnapshotSet,
    WorkflowExecution,
)
from esl_service.persistence.repository import ExecutionRepository
from esl_service.runtime.host import RunnerPorts
from tests.support.committed import (
    commit_configuration_version,
    committed_engine,
    purge_configuration_versions,
    purge_execution,
    session_factory,
)
from tests.support.run_one import CLOCK, KILLED_EXIT_CODE, POLICY
from tests.support.sources import ScriptedSources
from tests.support.tcp_proxy import CuttingProxy

WINDOW = SourceWindow(datetime(2026, 9, 2, 0, 0, tzinfo=UTC), datetime(2026, 9, 2, 0, 30, tzinfo=UTC))
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MARKER = "test-recovery-scenarios"


class PowerLoss(BaseException):
    """Stands in for the host vanishing: not an Exception, so nothing catches it."""


@dataclass
class Committed:
    """One committed engine, its configuration version, and the executions to purge."""

    engine: Engine
    configuration_version_id: UUID
    executions: list[UUID] = field(default_factory=list)

    def ports(self) -> RunnerPorts:
        return RunnerPorts(session_factory(self.engine))

    def runner(self, sources: ScriptedSources, ports: RunnerPorts | None = None) -> WorkflowRunner:
        ports = ports or self.ports()
        return WorkflowRunner(
            executions=ports,
            sources=sources,
            retry_policy=POLICY,
            persist=ports.persist,
            clock=lambda: CLOCK,
            jitter=lambda: 0.0,
        )

    def launch(self) -> UUID:
        launched = self.ports().launch_manual(
            ManualLaunch(requested_by="ops.alice", reason="CHG-21"),
            workflow_name="esl-refresh",
            store_code="084",
            mode=ExecutionMode.SHADOW,
            correlation_id=uuid4(),
            source_window_start=WINDOW.start,
            source_window_end=WINDOW.end,
            configuration_version_id=self.configuration_version_id,
            rule_version="compatibility-v1",
            now=CLOCK - timedelta(minutes=1),
        )
        assert launched.execution is not None
        self.executions.append(launched.execution.id)
        return launched.execution.id

    def session(self) -> Session:
        return session_factory(self.engine)()

    def execution(self, execution_id: UUID) -> WorkflowExecution:
        with self.session() as session:
            return session.get_one(WorkflowExecution, execution_id)

    def step_attempts(self, execution_id: UUID, step_name: str) -> list[int]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(ExecutionStep.attempt)
                    .where(ExecutionStep.execution_id == execution_id, ExecutionStep.step_name == step_name)
                    .order_by(ExecutionStep.attempt)
                )
            )

    def event_types(self, execution_id: UUID) -> list[str]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(ExecutionEvent.event_type)
                    .where(ExecutionEvent.execution_id == execution_id)
                    .order_by(ExecutionEvent.sequence)
                )
            )

    def counts(self, execution_id: UUID) -> tuple[int, int, int]:
        """(snapshot sets, reconciliation reports, actions) of one execution."""

        with self.session() as session:
            return (
                session.scalar(select(func.count()).select_from(SnapshotSet).where(SnapshotSet.execution_id == execution_id)) or 0,
                session.scalar(select(func.count()).select_from(ReconciliationReport).where(ReconciliationReport.execution_id == execution_id)) or 0,
                session.scalar(select(func.count()).select_from(RecordAction).where(RecordAction.execution_id == execution_id)) or 0,
            )

    def report(self, execution_id: UUID, *, now: datetime = CLOCK):
        with self.session() as session:
            return report_for(
                execution_id,
                executions=ExecutionRepository(session),
                actions=ActionRepository(session),
                now=now,
            )


@pytest.fixture
def committed(migrated_database_url: str) -> Iterator[Committed]:
    """Committed state for one scenario, purged afterwards whatever happened."""

    del migrated_database_url  # only to guarantee the schema is at head
    with committed_engine() as engine:
        state = Committed(engine, commit_configuration_version(engine, MARKER))
        try:
            yield state
        finally:
            for execution_id in state.executions:
                purge_execution(engine, execution_id)
            purge_configuration_versions(engine, MARKER)


def _run_in_a_separate_process(execution_id: UUID, *, die_after: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.support.run_one", "--execution", str(execution_id), "--die-after", die_after],
        cwd=_REPOSITORY_ROOT,
        env={**os.environ, "PYTHONPATH": str(_REPOSITORY_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# --- SQL Server unavailability ------------------------------------------------------


def test_sql_server_unavailability_is_retried_from_the_checkpoint_without_repeating_discovery(
    committed: Committed,
) -> None:
    sources = ScriptedSources()
    sources.fail_next(STEP_READ_STORE, FailureSignal(DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE))
    runner = committed.runner(sources)
    execution_id = committed.launch()

    first = runner.run(execution_id)

    assert first.status is ExecutionStatus.RETRY_WAIT
    waiting = committed.execution(execution_id)
    assert waiting.retry_not_before == CLOCK + timedelta(seconds=1)
    with committed.session() as session:
        repository = ExecutionRepository(session)
        assert execution_id not in repository.runnable_executions(limit=10, now=CLOCK)
        assert execution_id in repository.runnable_executions(limit=10, now=CLOCK + timedelta(seconds=1))
    report = committed.report(execution_id)
    assert report.resume_from == STEP_READ_STORE
    assert report.checkpoint is not None and report.checkpoint.startswith("read-warehouse:done")

    second = runner.run(execution_id)

    assert second.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS
    assert sources.calls.count(STEP_DISCOVER) == 1
    assert committed.step_attempts(execution_id, STEP_READ_STORE) == [1, 2]
    assert committed.counts(execution_id) == (1, 1, 1)
    assert committed.execution(execution_id).retry_not_before is None


# --- network interruption and AIMS unavailability (real faults on the local clone) ---


def _clone_url(name: str) -> str:
    raw = os.environ.get(name)
    if not raw:
        pytest.skip(f"{name} is required; see docs/development/aims-local-clone.md")
    url = make_url(raw)
    if url.host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(f"{name} must point at a local clone, never at AIMS itself")
    return raw


@pytest.fixture
def portal_proxy() -> Iterator[CuttingProxy]:
    url = make_url(_clone_url("ESL_TEST_AIMS_PORTAL_URL"))
    proxy = CuttingProxy(url.host or "127.0.0.1", url.port or 5432).start()
    try:
        yield proxy
    finally:
        proxy.stop()


def _reader_through(proxy: CuttingProxy) -> AimsCompatibilityReader:
    portal = (
        make_url(_clone_url("ESL_TEST_AIMS_PORTAL_URL"))
        .set(host="127.0.0.1", port=proxy.port)
        .update_query_dict({"connect_timeout": "2"})
    )
    core = make_url(_clone_url("ESL_TEST_AIMS_CORE_URL"))
    return AimsCompatibilityReader(create_read_only_engine(portal), create_read_only_engine(core))


def test_a_connection_cut_while_reading_aims_is_retryable_and_the_run_recovers(
    committed: Committed, portal_proxy: CuttingProxy
) -> None:
    """A real socket closed mid-result classifies as UNAVAILABLE, and a retry completes."""

    portal_proxy.mode, portal_proxy.cut_after_bytes = "cut", 256

    with pytest.raises(AimsUnavailable) as interrupted:
        _reader_through(portal_proxy).fetch_labels("084")

    signal = interrupted.value.signal
    assert signal == FailureSignal(DependencyKind.AIMS_COMPATIBILITY, FailureKind.UNAVAILABLE)
    assert classify(signal) is FailureClass.RETRYABLE
    assert portal_proxy.cuts >= 1

    sources = ScriptedSources()
    sources.fail_next(STEP_READ_STORE, signal)
    runner = committed.runner(sources)
    execution_id = committed.launch()
    assert runner.run(execution_id).status is ExecutionStatus.RETRY_WAIT
    assert committed.execution(execution_id).terminal_reason is None

    portal_proxy.mode = "pass"
    assert len(_reader_through(portal_proxy).fetch_labels("084")) > 0  # the dependency is back
    assert runner.run(execution_id).status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS


def test_a_refused_aims_connection_is_retryable_not_schema_drift(portal_proxy: CuttingProxy) -> None:
    portal_proxy.refuse()

    with pytest.raises(AimsUnavailable) as refused:
        _reader_through(portal_proxy).fetch_labels("084")

    assert refused.value.signal.kind is FailureKind.UNAVAILABLE
    assert classify(refused.value.signal) is FailureClass.RETRYABLE


# --- malformed data -------------------------------------------------------------------


def test_malformed_source_data_is_terminal_at_once_and_asks_for_correction(committed: Committed) -> None:
    sources = ScriptedSources()
    sources.fail_next(STEP_READ_WAREHOUSE, FailureSignal(DependencyKind.SOURCE_DATA, FailureKind.MALFORMED))
    runner = committed.runner(sources)
    execution_id = committed.launch()

    outcome = runner.run(execution_id)

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.terminal_reason == "read-warehouse:source_data:malformed:NON_RETRYABLE"
    assert committed.step_attempts(execution_id, STEP_READ_WAREHOUSE) == [1]  # never retried
    assert committed.counts(execution_id) == (0, 0, 0)  # no side effect
    with committed.session() as session:
        lease = ExecutionRepository(session).get_lease("esl-refresh:084")
        assert lease is not None and lease.released_at is not None
    report = committed.report(execution_id)
    assert report.next_operator_action.startswith("Correct source_data (malformed) through the approved process")


# --- partial completion and application restart (a real process death) ---------------


def test_partial_completion_resumes_after_the_last_checkpoint_without_duplicate_effects(
    committed: Committed,
) -> None:
    """Killed after canonicalize:done, before persist: the restart persists exactly once."""

    execution_id = committed.launch()

    killed = _run_in_a_separate_process(execution_id, die_after=f"{STEP_CANONICALIZE}:done")

    assert killed.returncode == KILLED_EXIT_CODE, killed.stderr
    assert committed.execution(execution_id).status == ExecutionStatus.RUNNING.value
    assert committed.counts(execution_id) == (0, 0, 0)

    sources = ScriptedSources()
    runner = committed.runner(sources)
    assert runner.recover_all() == (execution_id,)
    report = committed.report(execution_id)
    assert report.status == ExecutionStatus.RECOVERING.value
    assert report.resume_from == STEP_CANONICALIZE
    assert report.next_operator_action.startswith("None: startup recovery resumes from canonicalize")

    outcome = runner.run(execution_id)

    assert outcome.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS
    events = committed.event_types(execution_id)
    assert EVENT_RUN_RECOVERY_MARKED in events and EVENT_STEP_RESUMED in events
    assert sources.calls.count(STEP_DISCOVER) == 0  # restored from its checkpoint
    assert committed.step_attempts(execution_id, STEP_CANONICALIZE) == [1, 2]
    assert committed.step_attempts(execution_id, STEP_PERSIST) == [1]
    assert committed.counts(execution_id) == (1, 1, 1)


def test_an_application_restart_after_persist_completes_without_persisting_twice(
    committed: Committed,
) -> None:
    """Killed after persist:done, before the final transition: the guard of #104 holds."""

    execution_id = committed.launch()

    killed = _run_in_a_separate_process(execution_id, die_after=f"{STEP_PERSIST}:done")

    assert killed.returncode == KILLED_EXIT_CODE, killed.stderr
    assert committed.execution(execution_id).status == ExecutionStatus.RUNNING.value
    before = committed.counts(execution_id)
    assert before == (1, 1, 1)

    runner = committed.runner(ScriptedSources())
    runner.recover_all()
    outcome = runner.run(execution_id)

    assert outcome.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS
    assert outcome.persisted is not None and outcome.persisted.resumed is True
    assert committed.counts(execution_id) == before
    assert committed.step_attempts(execution_id, STEP_PERSIST) == [1, 2]


# --- server restart (simulated: a fresh engine over the same state) --------------------


def test_a_server_restart_is_recovered_by_a_fresh_engine_over_the_same_state(committed: Committed) -> None:
    """The first engine vanishes mid-step; a second one resumes and reuses the scope lease."""

    lost = ScriptedSources()
    lost.fail_next(STEP_READ_STORE, PowerLoss())
    first_engine = create_engine(committed.engine.url)  # the URL object keeps its password
    first_ports = RunnerPorts(session_factory(first_engine))
    execution_id = committed.launch()
    with pytest.raises(PowerLoss):
        committed.runner(lost, first_ports).run(execution_id)
    first_engine.dispose()  # the "server" is gone; nothing released the lease
    assert committed.execution(execution_id).status == ExecutionStatus.RUNNING.value

    restarted = committed.runner(ScriptedSources())
    assert restarted.recover_all() == (execution_id,)
    outcome = restarted.run(execution_id)

    assert outcome.status is ExecutionStatus.SUCCEEDED_WITH_EXCEPTIONS
    assert committed.step_attempts(execution_id, STEP_READ_STORE) == [1, 2]
    with committed.session() as session:
        lease = ExecutionRepository(session).get_lease("esl-refresh:084")
        assert lease is not None and lease.execution_id == execution_id and lease.released_at is not None
    assert committed.counts(execution_id) == (1, 1, 1)
