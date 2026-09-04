"""Composition root: wire settings, bundle, state store, scheduler, API (FR-029, #28).

Everything else in the service is built from injected ports; this is the one
module that knows how to obtain the real ones from ``Settings`` and the DPAPI
bundle. It is deliberately thin and untested by unit tests: each part it
assembles is tested on its own, and the assembly itself is exercised by
``esl-admin status`` and ``esl-admin serve`` against a real environment.

Transactions: the repositories expect a ``Session`` and never commit. The
API and the CLI need one transaction per operation, and the scheduler one per
launch, so ``TransactionalPorts`` runs each port method in its own
``session_factory.begin()`` block. Sessions do not expire on commit, so the
rows a method returns stay readable after the block closes.
"""

import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from esl_service.application.canonicalize import CanonicalizationResult
from esl_service.application.contracts import (
    BaselineReadResult,
    SourceWindow,
    StoreDirectoryEntry,
    StoreReadResult,
    UomMappingReadRequest,
    UomMappingReadResult,
    WarehouseReadRequest,
    WarehouseReadResult,
)
from esl_service.application.operations import AuthorizedOperations
from esl_service.application.persist_run import PersistedRun, RunContext, persist_run
from esl_service.application.run_evidence import (
    IssueEvidenceRow,
    MetricRunRow,
    ReconciliationReportRow,
    RunEvidenceRows,
    RunEvidenceService,
)
from esl_service.application.runner import RunOutcome, WorkflowRunner
from esl_service.config import (
    Settings,
    build_retry_policy,
    build_role_assignments,
    validate_startup_configuration,
)
from esl_service.domain.authorization import Principal
from esl_service.domain.operations import ExecutionQuery, ReplayRequest, RetryRequest
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.promotion_selection import SELECTION_STRATEGY_VERSION
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.scheduling import ManualLaunch
from esl_service.persistence.action_repository import ActionRepository
from esl_service.persistence.configuration_repository import ConfigurationRepository
from esl_service.persistence.db import create_database_engine_from_settings
from esl_service.persistence.evidence_repository import (
    PromotionEvidenceRepository,
    RecordOutcomeRepository,
)
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
from esl_service.persistence.run_evidence_repository import RunEvidenceRepository
from esl_service.persistence.snapshot_repository import SnapshotRepository
from esl_service.runtime.cli_operations import OperationsUnavailable
from esl_service.runtime.connectivity import SqlAlchemyConnector, build_probes
from esl_service.runtime.health import HealthService
from esl_service.runtime.identity import current_user_name
from esl_service.runtime.principals import current_principal
from esl_service.runtime.scheduler import LaunchContext, Scheduler
from esl_service.runtime.secrets import (
    BundleSecretProvider,
    DpapiSecretProvider,
    SecretUnavailableError,
)
from esl_service.runtime.service_host import ServiceHost
from esl_service.runtime.worker import WorkerLoop
from esl_service.web.auth import BearerTokenAuthenticator, tokens_from_bundle

#: Seconds between scheduler ticks; cron granularity is one minute.
TICK_INTERVAL_SECONDS = 60.0
#: Seconds between worker picks; a launched run waits at most this long.
WORKER_INTERVAL_SECONDS = 5.0


# --- settings and secrets ---------------------------------------------------------


def load_settings() -> Settings:
    settings, problems = validate_startup_configuration({})
    if settings is None:
        keys = ", ".join(problem.key for problem in problems) or "unknown"
        raise OperationsUnavailable(f"configuration is invalid: {keys}")
    return settings


def _secrets(settings: Settings) -> BundleSecretProvider:
    return DpapiSecretProvider(settings)


def _session_factory(settings: Settings) -> sessionmaker[Session]:
    try:
        engine = create_database_engine_from_settings(settings, _secrets(settings))
    except SecretUnavailableError:
        raise OperationsUnavailable(
            "state.password is not readable from the secret bundle; "
            "run `esl-admin secrets set state.password`"
        ) from None
    except ValueError as error:
        raise OperationsUnavailable(str(error)) from None
    return sessionmaker(engine, expire_on_commit=False)


# --- transactional ports ------------------------------------------------------------


class TransactionalPorts:
    """Every #26 port, each call in its own committed transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def _scope(self) -> AbstractContextManager[Session]:
        return self._sessions.begin()

    # LaunchPort
    def launch_manual(self, launch: ManualLaunch, **fields: Any) -> Any:
        with self._scope() as session:
            return LaunchRepository(session).launch_manual(launch, **fields)

    def launch_retry(self, execution_id: UUID, request: RetryRequest, *, correlation_id: UUID) -> Any:
        with self._scope() as session:
            return LaunchRepository(session).launch_retry(
                execution_id, request, correlation_id=correlation_id
            )

    def launch_replay(self, execution_id: UUID, request: ReplayRequest, *, correlation_id: UUID) -> Any:
        with self._scope() as session:
            return LaunchRepository(session).launch_replay(
                execution_id, request, correlation_id=correlation_id
            )

    # SchedulePort
    def schedules_for_scope(self, workflow_name: str, store_code: str | None) -> Sequence[Any]:
        with self._scope() as session:
            return LaunchRepository(session).schedules_for_scope(workflow_name, store_code)

    def set_schedule_enabled(self, schedule_id: UUID, *, enabled: bool, actor: str, reason: str) -> Any:
        with self._scope() as session:
            return LaunchRepository(session).set_schedule_enabled(
                schedule_id, enabled=enabled, actor=actor, reason=reason
            )

    # StatusPort
    def query_executions(self, query: ExecutionQuery) -> Sequence[Any]:
        with self._scope() as session:
            return ExecutionRepository(session).query_executions(query)

    # RunEvidencePort (#109)
    def issues_for(self, execution_id: UUID) -> Sequence[IssueEvidenceRow]:
        with self._scope() as session:
            return RunEvidenceRepository(session).issues_for(execution_id)

    def latest_report_for(
        self, execution_id: UUID
    ) -> ReconciliationReportRow | None:
        with self._scope() as session:
            return RunEvidenceRepository(session).latest_report_for(execution_id)

    def run_evidence_for(self, execution_id: UUID) -> RunEvidenceRows:
        with self._scope() as session:
            return RunEvidenceRepository(session).run_evidence_for(execution_id)

    def metric_evidence(self, *, per_scope_limit: int) -> Sequence[MetricRunRow]:
        with self._scope() as session:
            return RunEvidenceRepository(session).metric_evidence(
                per_scope_limit=per_scope_limit
            )

    # ReconciliationPort
    def finalize_report(
        self, execution_id: UUID, mode: ReconciliationMode, counts: ReconciliationCounts
    ) -> Any:
        with self._scope() as session:
            return ReconciliationRepository(session).finalize_report(execution_id, mode, counts)

    # AuditPort
    def append_audit_entry(self, **fields: Any) -> Any:
        with self._scope() as session:
            return ReconciliationRepository(session).append_audit_entry(**fields)

    # SchedulerPort
    def due_schedules(self, instant: datetime) -> Sequence[Any]:
        with self._scope() as session:
            return LaunchRepository(session).due_schedules(instant)

    def launch_scheduled(self, schedule_id: UUID, **fields: Any) -> Any:
        with self._scope() as session:
            return LaunchRepository(session).launch_scheduled(schedule_id, **fields)


# --- the tick loop -----------------------------------------------------------------------


class ThreadedTicker:
    """Calls ``scheduler.tick`` every interval on a daemon thread."""

    def __init__(
        self,
        scheduler: Scheduler,
        *,
        interval_seconds: float = TICK_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._interval = interval_seconds
        self._clock = clock
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="esl-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, deadline_seconds: float) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=deadline_seconds)
        return not thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._scheduler.tick(self._clock())
            except BaseException as error:  # noqa: BLE001 - the loop must survive one bad tick
                if self._on_error is not None:
                    self._on_error(error)


# --- builders used by the CLI and the service -----------------------------------------------


@dataclass(frozen=True)
class Host:
    settings: Settings
    ports: TransactionalPorts
    operations: AuthorizedOperations
    run_evidence: RunEvidenceService
    health: HealthService
    scheduler: Scheduler
    ticker: ThreadedTicker
    service: ServiceHost
    context: LaunchContext
    authenticator: BearerTokenAuthenticator
    worker: WorkerLoop | None = None


def launch_context(settings: Settings | None = None) -> LaunchContext:
    """Register the active configuration and return what every run records."""

    settings = settings or load_settings()
    sessions = _session_factory(settings)
    with sessions.begin() as session:
        version = ConfigurationRepository(session).ensure_active(
            settings, activated_by=current_user_name()
        )
        version_id = version.id
    return LaunchContext(
        mode=ExecutionMode.SHADOW if settings.shadow_mode else ExecutionMode.ACTIVE,
        configuration_version_id=version_id,
        rule_version=SELECTION_STRATEGY_VERSION,
    )


def build_operations(settings: Settings | None = None) -> AuthorizedOperations:
    settings = settings or load_settings()
    ports = TransactionalPorts(_session_factory(settings))
    return AuthorizedOperations(
        launches=ports, schedules=ports, status=ports, reconciliation=ports, audit=ports
    )


def build_run_evidence(settings: Settings | None = None) -> RunEvidenceService:
    settings = settings or load_settings()
    ports = TransactionalPorts(_session_factory(settings))
    operations = AuthorizedOperations(
        launches=ports,
        schedules=ports,
        status=ports,
        reconciliation=ports,
        audit=ports,
    )
    return RunEvidenceService(
        operations,
        ports,
        clock=lambda: datetime.now(UTC),
        metrics_run_limit=settings.metrics_run_limit,
    )


def build_principal(settings: Settings | None = None) -> Principal:
    return current_principal(settings or load_settings())


def build_health(settings: Settings | None = None) -> HealthService:
    settings = settings or load_settings()
    return HealthService(build_probes(settings, _secrets(settings), SqlAlchemyConnector()))


def build_host(settings: Settings | None = None) -> Host:
    settings = settings or load_settings()
    ports = TransactionalPorts(_session_factory(settings))
    context = launch_context(settings)
    scheduler = Scheduler(ports, context)
    ticker = ThreadedTicker(scheduler)
    worker = build_worker(settings)
    service = ServiceHost(scheduler=scheduler, ticker=ticker, audit=ports, worker=worker)
    operations = AuthorizedOperations(
        launches=ports, schedules=ports, status=ports, reconciliation=ports, audit=ports
    )
    run_evidence = RunEvidenceService(
        operations,
        ports,
        clock=lambda: datetime.now(UTC),
        metrics_run_limit=settings.metrics_run_limit,
    )
    try:
        tokens = tokens_from_bundle(_secrets(settings))
    except SecretUnavailableError:
        raise OperationsUnavailable("the secret bundle is unreadable; API tokens cannot be loaded") from None
    authenticator = BearerTokenAuthenticator(tokens, build_role_assignments(settings))
    return Host(
        settings=settings,
        ports=ports,
        operations=operations,
        run_evidence=run_evidence,
        health=build_health(settings),
        scheduler=scheduler,
        ticker=ticker,
        service=service,
        context=context,
        authenticator=authenticator,
        worker=worker,
    )


def run_foreground(settings: Settings | None = None) -> None:
    """Start the host and serve the API until interrupted (``esl-admin serve``)."""

    import uvicorn

    from esl_service.web.app import create_app

    host = build_host(settings)
    app = create_app(
        operations=host.operations,
        authenticator=host.authenticator,
        health=host.health,
        scheduler=host.scheduler,
        audit=host.ports,
        run_evidence=host.run_evidence,
        configuration_version_id=host.context.configuration_version_id,
        mode=host.context.mode,
    )
    host.service.start()
    try:
        uvicorn.run(app, host=host.settings.internal_host, port=host.settings.internal_port, log_level="info")
    finally:
        host.service.stop(reason="foreground exit")


# --- the runner (#102) ----------------------------------------------------------------------


class RunnerPorts(TransactionalPorts):
    """The runner's execution port: every call in its own committed transaction.

    A run spans minutes, so it must not hold one transaction; each transition,
    step, checkpoint, and heartbeat commits on its own, which is also what
    makes the evidence visible to ``runs show`` while the run is in flight.
    """

    def get_execution(self, execution_id: UUID) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).get_execution(execution_id)

    def transition_execution(
        self,
        execution_id: UUID,
        expected_status: Any,
        requested_status: Any,
        *,
        terminal_reason: str | None = None,
        retry_not_before: datetime | None = None,
    ) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).transition_execution(
                execution_id,
                expected_status,
                requested_status,
                terminal_reason=terminal_reason,
                retry_not_before=retry_not_before,
            )

    def start_step(self, execution_id: UUID, step_name: str, *, attempt: int = 1) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).start_step(execution_id, step_name, attempt=attempt)

    def finish_step(self, step_id: UUID, *, outcome: str, failure_class: Any = None) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).finish_step(step_id, outcome=outcome, failure_class=failure_class)

    def append_checkpoint(self, step_id: UUID, **fields: Any) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).append_checkpoint(step_id, **fields)

    def append_event(self, execution_id: UUID, event_type: str, payload: Any) -> Any:
        with self._scope() as session:
            return ExecutionRepository(session).append_event(execution_id, event_type, payload)

    def step_history(self, execution_id: UUID) -> Sequence[Any]:
        with self._scope() as session:
            return ExecutionRepository(session).step_history(execution_id)

    def configuration_hash_of(self, configuration_version_id: UUID) -> str:
        with self._scope() as session:
            return ExecutionRepository(session).configuration_hash_of(configuration_version_id)

    def heartbeat_scope(self, scope_key: str, execution_id: UUID) -> bool:
        with self._scope() as session:
            return ExecutionRepository(session).heartbeat_scope(scope_key, execution_id)

    def release_scope(self, scope_key: str, execution_id: UUID) -> bool:
        with self._scope() as session:
            return ExecutionRepository(session).release_scope(scope_key, execution_id)

    def recoverable_executions(self) -> Sequence[Any]:
        with self._scope() as session:
            return ExecutionRepository(session).recoverable_executions()

    def runnable_executions(self, limit: int) -> Sequence[UUID]:
        with self._scope() as session:
            return ExecutionRepository(session).runnable_executions(limit=limit, now=datetime.now(UTC))

    def persist(
        self,
        result: CanonicalizationResult,
        context: RunContext,
        *,
        legacy_baseline: BaselineReadResult | None = None,
        step_id: UUID | None = None,
    ) -> PersistedRun:
        """The #104 step in one transaction, as its checkpoint semantics require."""

        with self._scope() as session:
            return persist_run(
                result,
                context,
                executions=ExecutionRepository(session),
                snapshots=SnapshotRepository(session),
                outcomes=RecordOutcomeRepository(session),
                promotions=PromotionEvidenceRepository(session),
                actions=ActionRepository(session),
                reconciliation=ReconciliationRepository(session),
                legacy_baseline=legacy_baseline,
                step_id=step_id,
            )


class LiveSources:
    """The four tiers behind their adapters (#91 to #94), built per call.

    Each adapter raises its own error carrying a #20 ``FailureSignal``; the
    runner classifies it. Readers are built per call so a rotated credential
    or a changed DimStore row is picked up by the next run, not the next
    restart.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._secrets = _secrets(settings)

    def discover_store(self, store_code: str, window: SourceWindow) -> StoreDirectoryEntry | None:
        from esl_service.adapters.warehouse import WarehouseReader

        directory = WarehouseReader.from_settings(self._settings, self._secrets).discover_stores(window)
        for entry in directory.stores:
            if entry.store_code == store_code:
                return entry
        return None

    def read_warehouse(self, store_code: str, window: SourceWindow) -> WarehouseReadResult:
        from esl_service.adapters.warehouse import WarehouseReader

        return WarehouseReader.from_settings(self._settings, self._secrets).read_store(
            WarehouseReadRequest(store_code, window)
        )

    def read_uom_mappings(self, item_codes: Sequence[str], window: SourceWindow) -> UomMappingReadResult:
        from esl_service.adapters.pepito_ho import PepitoHoReader

        return PepitoHoReader.from_settings(self._settings, self._secrets).read_mappings(
            UomMappingReadRequest(tuple(item_codes), window)
        )

    def read_store(self, entry: StoreDirectoryEntry, window: SourceWindow) -> StoreReadResult:
        from esl_service.adapters.store import StoreReader
        from esl_service.application.contracts import StoreReadRequest

        return StoreReader.from_directory_entry(self._settings, self._secrets, entry).read_store(
            StoreReadRequest(entry, window)
        )

    def read_baseline(self, store_code: str, window: SourceWindow) -> BaselineReadResult | None:
        from esl_service.adapters.legacy_baseline import TbEslBaselineReader
        from esl_service.application.contracts import BaselineReadRequest

        if not self._settings.shadow_mode:
            return None
        return TbEslBaselineReader.from_settings(self._settings, self._secrets).read_baseline(
            BaselineReadRequest(store_code, window)
        )


def build_runner(settings: Settings | None = None) -> tuple[WorkflowRunner, RunnerPorts]:
    settings = settings or load_settings()
    ports = RunnerPorts(_session_factory(settings))
    runner = WorkflowRunner(
        executions=ports,
        sources=LiveSources(settings),
        retry_policy=build_retry_policy(settings),
        persist=ports.persist,
    )
    return runner, ports


def build_worker(settings: Settings | None = None) -> WorkerLoop:
    """The run loop: recovery first, then runnable executions under the bound."""

    settings = settings or load_settings()
    runner, ports = build_runner(settings)

    def run(execution_id: UUID) -> RunOutcome:
        return runner.run(execution_id)

    return WorkerLoop(
        ports.runnable_executions,
        run,
        concurrency=settings.worker_concurrency,
        interval_seconds=WORKER_INTERVAL_SECONDS,
        on_start=runner.recover_all,
    )


def execution_steps(execution_id: UUID, settings: Settings | None = None) -> Sequence[Any]:
    """The steps and checkpoints of one run, for ``runs show``."""

    settings = settings or load_settings()
    return RunnerPorts(_session_factory(settings)).step_history(execution_id)
