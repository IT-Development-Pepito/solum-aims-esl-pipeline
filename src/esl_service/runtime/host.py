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

from esl_service.application.operations import AuthorizedOperations
from esl_service.config import (
    Settings,
    build_role_assignments,
    validate_startup_configuration,
)
from esl_service.domain.authorization import Principal
from esl_service.domain.operations import ExecutionQuery, ReplayRequest, RetryRequest
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.promotion_selection import SELECTION_STRATEGY_VERSION
from esl_service.domain.reconciliation import ReconciliationCounts, ReconciliationMode
from esl_service.domain.scheduling import ManualLaunch
from esl_service.persistence.configuration_repository import ConfigurationRepository
from esl_service.persistence.db import create_database_engine_from_settings
from esl_service.persistence.launch_repository import LaunchRepository
from esl_service.persistence.reconciliation_repository import ReconciliationRepository
from esl_service.persistence.repository import ExecutionRepository
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
from esl_service.web.auth import BearerTokenAuthenticator, tokens_from_bundle

#: Seconds between scheduler ticks; cron granularity is one minute.
TICK_INTERVAL_SECONDS = 60.0


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
    health: HealthService
    scheduler: Scheduler
    ticker: ThreadedTicker
    service: ServiceHost
    context: LaunchContext
    authenticator: BearerTokenAuthenticator


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
    service = ServiceHost(scheduler=scheduler, ticker=ticker, audit=ports)
    operations = AuthorizedOperations(
        launches=ports, schedules=ports, status=ports, reconciliation=ports, audit=ports
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
        health=build_health(settings),
        scheduler=scheduler,
        ticker=ticker,
        service=service,
        context=context,
        authenticator=authenticator,
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
        configuration_version_id=host.context.configuration_version_id,
        mode=host.context.mode,
    )
    host.service.start()
    try:
        uvicorn.run(app, host=host.settings.internal_host, port=host.settings.internal_port, log_level="info")
    finally:
        host.service.stop(reason="foreground exit")
