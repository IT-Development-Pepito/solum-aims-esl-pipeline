"""The internal operations API (FR-029, #28).

Every mutating route is the FR-023 operation of the same name, reached
through ``AuthorizedOperations`` (#26). The API therefore adds only two
things: bearer-token authentication (AD-019) and HTTP shapes. A missing or
unknown token is 401; a role refusal is 403 and is already in the audit
ledger when the response is built, because the service wrote it before
raising; a malformed request is 422 before any role check; an unknown run or
schedule is 404. Health routes need no token so a monitor can ask whether the
process is alive without holding an operator credential, and the OpenAPI
document is served only to authenticated callers.

The listener itself is bound by the host to ``ESL_INTERNAL_HOST`` and
``ESL_INTERNAL_PORT`` only (architecture section 6); nothing here is
reachable from a public interface.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from esl_service.application.operations import (
    AuditPort,
    AuthorizedOperations,
    FallbackOutcome,
    InvalidOperationRequest,
)
from esl_service.application.run_evidence import (
    IssueQuery,
    ReportQuery,
    RunEvidenceService,
)
from esl_service.domain.authorization import NotAuthorized, Operation, Principal
from esl_service.domain.operations import (
    ExecutionQuery,
    InvalidExecutionQuery,
    InvalidWorkflowControl,
)
from esl_service.domain.outcomes import ExecutionMode
from esl_service.domain.scheduling import InvalidManualLaunch
from esl_service.runtime.health import HealthService
from esl_service.runtime.scheduler import Scheduler
from esl_service.web.audit_schemas import (
    ReconciliationReportResponse,
    RecoveryResponse,
    RunIssuesResponse,
    StepEvidenceResponse,
)
from esl_service.web.auth import AuthenticationFailed, BearerTokenAuthenticator
from esl_service.web.metrics import render_metrics

SCHEDULER_PAUSED = "scheduler.paused"
SCHEDULER_RESUMED = "scheduler.resumed"
SCHEDULER_RESOURCE = "scheduler"


# --- request and response shapes ---------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class ReasonRequest(_Strict):
    reason: str

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class WindowRequest(ReasonRequest):
    source_window_start: datetime
    source_window_end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "WindowRequest":
        _aware(self.source_window_start, "source_window_start")
        _aware(self.source_window_end, "source_window_end")
        if self.source_window_start > self.source_window_end:
            raise ValueError("source_window_start must not follow source_window_end")
        return self


class RunRequest(WindowRequest):
    workflow_name: str
    store_code: str

    @field_validator("workflow_name", "store_code")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class FallbackRequest(ReasonRequest):
    workflow_name: str
    store_code: str | None = None


class ExecutionView(BaseModel):
    """The operator-facing shape of one run; identifiers and states only."""

    model_config = ConfigDict(extra="forbid", from_attributes=True, frozen=True)

    id: UUID
    workflow_name: str
    store_code: str
    trigger_type: str
    mode: str
    correlation_id: UUID
    source_window_start: datetime
    source_window_end: datetime
    configuration_version_id: UUID
    rule_version: str
    requested_by: str | None
    reason: str | None
    retry_of_execution_id: UUID | None
    replay_of_execution_id: UUID | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    terminal_reason: str | None


class ExecutionDetailView(ExecutionView):
    """One run plus its operator timeline and four-field recovery guidance."""

    steps: tuple[StepEvidenceResponse, ...]
    recovery: RecoveryResponse


class LaunchResponse(_Strict):
    launched: bool
    execution: ExecutionView | None
    refusal: str | None


class ScheduleView(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    workflow_name: str
    store_code: str
    enabled: bool


class FallbackResponse(_Strict):
    disabled_schedule_ids: list[UUID]
    already_disabled: list[UUID]
    applied_at: datetime


class SchedulerView(_Strict):
    paused: bool


class DependencyView(_Strict):
    name: str
    state: str
    required: bool
    detail: str | None


class ReadinessView(_Strict):
    ready: bool
    dependencies: list[DependencyView]


# --- the application ------------------------------------------------------------


def _launch_response(result: Any) -> LaunchResponse:
    refusal: str | None = None
    for name in ("schedule_refusal", "control_refusal"):
        value = getattr(result, name, None)
        if value is not None:
            refusal = str(getattr(value, "value", value))
    ownership = getattr(result, "ownership", None)
    if refusal is None and ownership is not None and not result.launched:
        refusal = "SCOPE_OWNED"
    execution = result.execution
    return LaunchResponse(
        launched=result.launched,
        execution=ExecutionView.model_validate(execution) if execution is not None else None,
        refusal=refusal,
    )


def create_app(
    *,
    operations: AuthorizedOperations,
    authenticator: BearerTokenAuthenticator,
    health: HealthService,
    scheduler: Scheduler,
    audit: AuditPort,
    run_evidence: RunEvidenceService,
    configuration_version_id: UUID,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    mode: ExecutionMode = ExecutionMode.SHADOW,
) -> FastAPI:
    """Build the API over an already-wired service; nothing here opens a connection."""

    app = FastAPI(title="SOLUM ESL pipeline operations", openapi_url=None, docs_url=None, redoc_url=None)

    def principal(request: Request) -> Principal:
        try:
            return authenticator.authenticate(request.headers.get("authorization"))
        except AuthenticationFailed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    Caller = Annotated[Principal, Depends(principal)]

    @app.exception_handler(NotAuthorized)
    def _refused(_: Request, error: NotAuthorized) -> JSONResponse:
        decision = error.decision
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "detail": str(error),
                "identity": decision.identity,
                "operation": decision.operation.value,
                "required_role": decision.required_role.value,
                "policy_version": decision.policy_version,
            },
        )

    @app.exception_handler(InvalidOperationRequest)
    @app.exception_handler(InvalidExecutionQuery)
    @app.exception_handler(InvalidWorkflowControl)
    @app.exception_handler(InvalidManualLaunch)
    def _invalid(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(LookupError)
    def _missing(_: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    # -- health, no token -------------------------------------------------------

    @app.get("/health/live")
    def live() -> dict[str, bool]:
        return {"alive": health.liveness()}

    @app.get("/health/ready", response_model=ReadinessView)
    def ready() -> JSONResponse:
        view = ReadinessView(
            ready=health.readiness(),
            dependencies=[
                DependencyView(name=d.name, state=d.state.value, required=d.required, detail=d.detail)
                for d in health.dependency_health()
            ],
        )
        code = status.HTTP_200_OK if view.ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content=view.model_dump(mode="json"))

    # -- runs ---------------------------------------------------------------------

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=LaunchResponse)
    def trigger(body: RunRequest, caller: Caller) -> LaunchResponse:
        from uuid import uuid4

        result = operations.trigger(
            caller,
            body.reason,
            workflow_name=body.workflow_name,
            store_code=body.store_code,
            mode=mode,
            correlation_id=uuid4(),
            source_window_start=body.source_window_start,
            source_window_end=body.source_window_end,
            configuration_version_id=configuration_version_id,
            rule_version=_rule_version(),
        )
        return _launch_response(result)

    @app.get("/runs", response_model=list[ExecutionView])
    def list_runs(
        caller: Caller,
        execution_id: Annotated[UUID | None, Query()] = None,
        workflow_name: Annotated[str | None, Query()] = None,
        store_code: Annotated[str | None, Query()] = None,
        started_from: Annotated[datetime | None, Query()] = None,
        started_to: Annotated[datetime | None, Query()] = None,
    ) -> list[ExecutionView]:
        query = ExecutionQuery(
            execution_id=execution_id,
            workflow_name=workflow_name,
            store_code=store_code,
            started_from=started_from,
            started_to=started_to,
        )
        return [ExecutionView.model_validate(e) for e in operations.status(caller, query)]

    @app.get("/runs/{execution_id}/issues", response_model=RunIssuesResponse)
    def run_issues(
        execution_id: UUID,
        caller: Caller,
        code: Annotated[str | None, Query()] = None,
        severity: Annotated[str | None, Query()] = None,
        item: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunIssuesResponse:
        result = run_evidence.issues(
            caller,
            execution_id,
            IssueQuery(code=code, severity=severity, item=item, limit=limit, offset=offset),
        )
        return RunIssuesResponse.model_validate(result)

    @app.get("/runs/{execution_id}/report", response_model=ReconciliationReportResponse)
    def run_report(
        execution_id: UUID,
        caller: Caller,
        category: Annotated[str | None, Query()] = None,
        item: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ReconciliationReportResponse:
        result = run_evidence.report(
            caller,
            execution_id,
            ReportQuery(category=category, item=item, limit=limit, offset=offset),
        )
        return ReconciliationReportResponse.model_validate(result)

    @app.get("/runs/{execution_id}", response_model=ExecutionDetailView)
    def show_run(execution_id: UUID, caller: Caller) -> ExecutionDetailView:
        detail = run_evidence.run_detail(caller, execution_id)
        execution = ExecutionView.model_validate(detail.execution)
        return ExecutionDetailView.model_validate(
            {
                **execution.model_dump(),
                "steps": detail.steps,
                "recovery": detail.recovery,
            }
        )

    @app.get("/metrics")
    def metrics(caller: Caller) -> Response:
        body, content_type = render_metrics(run_evidence.metrics(caller))
        return Response(content=body, headers={"Content-Type": content_type})

    @app.post("/runs/{execution_id}/retry", status_code=202, response_model=LaunchResponse)
    def retry(execution_id: UUID, body: ReasonRequest, caller: Caller) -> LaunchResponse:
        from uuid import uuid4

        return _launch_response(
            operations.retry(caller, execution_id, body.reason, correlation_id=uuid4())
        )

    @app.post("/runs/{execution_id}/replay", status_code=202, response_model=LaunchResponse)
    def replay(execution_id: UUID, body: WindowRequest, caller: Caller) -> LaunchResponse:
        from uuid import uuid4

        return _launch_response(
            operations.replay(
                caller,
                execution_id,
                body.reason,
                correlation_id=uuid4(),
                source_window_start=body.source_window_start,
                source_window_end=body.source_window_end,
            )
        )

    # -- schedules and fallback -------------------------------------------------------

    @app.post("/schedules/{schedule_id}/enable", response_model=ScheduleView)
    def enable_schedule(schedule_id: UUID, body: ReasonRequest, caller: Caller) -> ScheduleView:
        return ScheduleView.model_validate(operations.enable_schedule(caller, schedule_id, body.reason))

    @app.post("/schedules/{schedule_id}/disable", response_model=ScheduleView)
    def disable_schedule(schedule_id: UUID, body: ReasonRequest, caller: Caller) -> ScheduleView:
        return ScheduleView.model_validate(operations.disable_schedule(caller, schedule_id, body.reason))

    @app.post("/fallback", response_model=FallbackResponse)
    def fallback(body: FallbackRequest, caller: Caller) -> FallbackResponse:
        outcome: FallbackOutcome = operations.fallback(
            caller, body.reason, workflow_name=body.workflow_name, store_code=body.store_code
        )
        return FallbackResponse(
            disabled_schedule_ids=list(outcome.disabled_schedule_ids),
            already_disabled=list(outcome.already_disabled),
            applied_at=outcome.applied_at,
        )

    # -- scheduler lifecycle over the API ------------------------------------------------

    @app.get("/scheduler", response_model=SchedulerView)
    def scheduler_state(caller: Caller) -> SchedulerView:
        operations.authorize(caller, Operation.STATUS, resource_key=SCHEDULER_RESOURCE)
        return SchedulerView(paused=scheduler.paused)

    def _set_paused(caller: Principal, reason: str, *, paused: bool) -> SchedulerView:
        operation = Operation.SCHEDULE_DISABLE if paused else Operation.SCHEDULE_ENABLE
        operations.authorize(caller, operation, resource_key=SCHEDULER_RESOURCE)
        was_paused = scheduler.paused
        if paused:
            scheduler.pause()
        else:
            scheduler.resume()
        audit.append_audit_entry(
            actor=caller.identity,
            action=SCHEDULER_PAUSED if paused else SCHEDULER_RESUMED,
            reason=reason,
            resource_type=SCHEDULER_RESOURCE,
            resource_key=SCHEDULER_RESOURCE,
            outcome="APPLIED",
            before_evidence={"paused": was_paused},
            after_evidence={"paused": paused, "at": clock().isoformat()},
        )
        return SchedulerView(paused=scheduler.paused)

    @app.post("/scheduler/pause", response_model=SchedulerView)
    def pause_scheduler(body: ReasonRequest, caller: Caller) -> SchedulerView:
        return _set_paused(caller, body.reason, paused=True)

    @app.post("/scheduler/resume", response_model=SchedulerView)
    def resume_scheduler(body: ReasonRequest, caller: Caller) -> SchedulerView:
        return _set_paused(caller, body.reason, paused=False)

    # -- the contract, to authenticated callers only --------------------------------------

    @app.get("/openapi.json", include_in_schema=False)
    def openapi(caller: Caller) -> dict[str, Any]:
        return app.openapi()

    return app


def _rule_version() -> str:
    from esl_service.domain.promotion_selection import SELECTION_STRATEGY_VERSION

    return SELECTION_STRATEGY_VERSION
