"""Operator commands on the command line (FR-029, #28).

The CLI is the second surface over the same #26 service the API uses. The
principal is the running Windows account under ``ESL_OPERATOR_ROLES``
(AD-018), every mutation needs ``--reason``, and a role refusal exits with
``EXIT_NOT_AUTHORIZED`` after the service has audited it. ``status`` reports
the same health the API serves, and ``serve`` runs the host in the
foreground for development and diagnostics; production runs it as the
Windows Service.

The service, principal, and health report are reached through module-level
factories so tests replace them without a database or the Windows API. The
defaults build everything from ``Settings`` and the DPAPI bundle; when that
is impossible the command says why by category, never with a traceback.
"""

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

import typer

from esl_service.application.operations import (
    AuthorizedOperations,
    InvalidOperationRequest,
)
from esl_service.application.run_evidence import (
    EvidenceWithheld,
    IssueQuery,
    IssueRead,
    ReportQuery,
    ReportRead,
    RunDetailRead,
    RunEvidenceService,
)
from esl_service.domain.authorization import NotAuthorized, Principal
from esl_service.domain.operations import (
    ExecutionQuery,
    InvalidExecutionQuery,
    InvalidWorkflowControl,
)
from esl_service.domain.scheduling import InvalidManualLaunch
from esl_service.runtime.health import HealthService
from esl_service.runtime.scheduler import LaunchContext

#: A refused role is neither success nor an ordinary failure.
EXIT_NOT_AUTHORIZED = 3
#: Stored evidence carried a secret-like key and was withheld (NFR-009).
EXIT_EVIDENCE_WITHHELD = 4


class OperationsUnavailable(RuntimeError):
    """Raised when the service cannot be built: configuration, bundle, or state store."""


# --- seams replaced by tests -----------------------------------------------------


def _default_operations() -> AuthorizedOperations:
    from esl_service.runtime.host import build_operations

    return build_operations()


def _default_principal() -> Principal:
    from esl_service.runtime.host import build_principal

    return build_principal()


def _default_health() -> HealthService:
    from esl_service.runtime.host import build_health

    return build_health()


_operations: Callable[[], AuthorizedOperations] = _default_operations
_principal: Callable[[], Principal] = _default_principal
_health: Callable[[], HealthService] = _default_health


# --- helpers ---------------------------------------------------------------------


Reason = Annotated[str, typer.Option("--reason", help="Change or incident ticket; recorded in the audit trail.")]
# Typer's own datetime parser accepts no UTC offset, and a window without one
# is ambiguous, so the options are taken as text and parsed here.
WindowStart = Annotated[
    str, typer.Option("--window-start", help="Source window start, ISO 8601 with offset.")
]
WindowEnd = Annotated[str, typer.Option("--window-end", help="Source window end, ISO 8601 with offset.")]


def _aware(text: str, name: str) -> datetime:
    """Parse an ISO 8601 instant that must carry a timezone offset."""

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        typer.echo(f"Refused: {name} must be an ISO 8601 instant, for example 2026-09-02T07:00:00+07:00")
        raise typer.Exit(code=1) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        typer.echo(f"Refused: {name} must carry a timezone offset, for example 2026-09-02T07:00:00+07:00")
        raise typer.Exit(code=1)
    return moment


def _default_context() -> LaunchContext:
    from esl_service.runtime.host import launch_context

    return launch_context()


_context: Callable[[], LaunchContext] = _default_context


def _default_run_evidence() -> RunEvidenceService:
    from esl_service.runtime.host import build_run_evidence

    return build_run_evidence()


_run_evidence: Callable[[], RunEvidenceService] = _default_run_evidence


def _service() -> tuple[AuthorizedOperations, Principal]:
    try:
        return _operations(), _principal()
    except OperationsUnavailable as error:
        typer.echo(f"Unavailable: {error}")
        raise typer.Exit(code=1) from None


def _evidence_service() -> tuple[RunEvidenceService, Principal]:
    try:
        return _run_evidence(), _principal()
    except OperationsUnavailable as error:
        typer.echo(f"Unavailable: {error}")
        raise typer.Exit(code=1) from None


def _run(action: Callable[[], object]) -> object:
    """Run one operation, translating the service's refusals into exit codes."""

    try:
        return action()
    except NotAuthorized as error:
        typer.echo(f"Refused: {error}")
        raise typer.Exit(code=EXIT_NOT_AUTHORIZED) from None
    except EvidenceWithheld as error:
        typer.echo(f"Evidence withheld: {error}")
        raise typer.Exit(code=EXIT_EVIDENCE_WITHHELD) from None
    except (
        InvalidOperationRequest,
        InvalidWorkflowControl,
        InvalidExecutionQuery,
        InvalidManualLaunch,
    ) as error:
        typer.echo(f"Invalid request: {error}")
        raise typer.Exit(code=1) from None
    except LookupError as error:
        typer.echo(f"Not found: {error}")
        raise typer.Exit(code=1) from None


def _print_execution(execution: object) -> None:
    for name in (
        "id",
        "workflow_name",
        "store_code",
        "trigger_type",
        "mode",
        "status",
        "source_window_start",
        "source_window_end",
        "requested_by",
        "reason",
        "started_at",
        "ended_at",
        "terminal_reason",
    ):
        typer.echo(f"{name}: {getattr(execution, name, None)}")


def _print_launch(result: object) -> None:
    execution = getattr(result, "execution", None)
    if execution is None:
        typer.echo("Not launched.")
        for name in ("schedule_refusal", "control_refusal", "ownership"):
            value = getattr(result, name, None)
            if value is not None:
                typer.echo(f"{name}: {getattr(value, 'value', value)}")
        raise typer.Exit(code=1)
    typer.echo("Launched.")
    _print_execution(execution)


# --- status --------------------------------------------------------------------------


def status() -> None:
    """Report liveness, readiness, and each dependency's health."""

    try:
        health = _health()
    except OperationsUnavailable as error:
        typer.echo(f"Unavailable: {error}")
        raise typer.Exit(code=1) from None
    ready = health.readiness()
    typer.echo(f"ready: {'yes' if ready else 'no'}")
    for dependency in health.dependency_health():
        detail = f"  {dependency.detail}" if dependency.detail else ""
        required = "required" if dependency.required else "optional"
        typer.echo(f"{dependency.name:<20} {dependency.state.value:<12} {required}{detail}")
    if not ready:
        raise typer.Exit(code=1)


# --- runs -------------------------------------------------------------------------------

runs_app = typer.Typer(no_args_is_help=True, help="Trigger, inspect, retry, and replay runs.")


@runs_app.command("start")
def runs_start(
    workflow: Annotated[str, typer.Option("--workflow")],
    store: Annotated[str, typer.Option("--store")],
    reason: Reason,
    window_start: WindowStart,
    window_end: WindowEnd,
) -> None:
    """Launch one run for a workflow and store under your own account."""

    start = _aware(window_start, "--window-start")
    end = _aware(window_end, "--window-end")
    operations, principal = _service()
    try:
        context = _context()
    except OperationsUnavailable as error:
        typer.echo(f"Unavailable: {error}")
        raise typer.Exit(code=1) from None
    result = _run(
        lambda: operations.trigger(
            principal,
            reason,
            workflow_name=workflow,
            store_code=store,
            mode=context.mode,
            correlation_id=uuid4(),
            source_window_start=start,
            source_window_end=end,
            configuration_version_id=context.configuration_version_id,
            rule_version=context.rule_version,
        )
    )
    _print_launch(result)


@runs_app.command("show")
def runs_show(execution_id: UUID) -> None:
    """Print one run by id."""

    # One authorized read serves the row, the steps, and the recovery fields.
    evidence, principal = _evidence_service()
    detail = _run(lambda: evidence.run_detail(principal, execution_id))
    assert isinstance(detail, RunDetailRead)
    _print_execution(detail.execution)
    _print_steps(detail.steps)
    _print_recovery(detail.recovery)


def _print_steps(steps: Sequence[object]) -> None:
    """Show where the run is: each step's latest attempt and its last checkpoint (#102)."""

    if not steps:
        typer.echo("steps: none yet")
        return
    for step in steps:
        failure = getattr(step, "failure_class", None)
        detail = f", {failure}" if failure else ""
        typer.echo(
            f"step {getattr(step, 'step_name', '?')}: {getattr(step, 'outcome', '?')} "
            f"(attempt {getattr(step, 'attempt', '?')}{detail})"
        )
        typer.echo(f"  duration_seconds: {getattr(step, 'duration_seconds', None)}")
        checkpoint_key = getattr(step, "checkpoint_key", None)
        if checkpoint_key is not None:
            typer.echo(
                f"  checkpoint {checkpoint_key} @ {getattr(step, 'checkpoint_watermark', None)}"
            )
        counts = getattr(step, "checkpoint_counts", {})
        if counts:
            typer.echo(f"  checkpoint_counts: {json.dumps(counts, sort_keys=True)}")


def _print_recovery(recovery: object) -> None:
    typer.echo(f"recovery_scope: {getattr(recovery, 'scope', None)}")
    typer.echo(f"recovery_checkpoint: {getattr(recovery, 'checkpoint', None)}")
    typer.echo(f"recovery_resume_from: {getattr(recovery, 'resume_from', None)}")
    uncertainty = getattr(recovery, "external_uncertainty", ())
    typer.echo(f"recovery_external_uncertainty: {len(uncertainty)}")
    typer.echo(f"recovery_next_action: {getattr(recovery, 'next_operator_action', None)}")


@runs_app.command("issues")
def runs_issues(
    execution_id: UUID,
    code: Annotated[str | None, typer.Option("--code")] = None,
    severity: Annotated[str | None, typer.Option("--severity")] = None,
    item: Annotated[str | None, typer.Option("--item")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
) -> None:
    """Summarize one run's issue codes and optionally drill into records."""

    evidence, principal = _evidence_service()
    result = _run(
        lambda: evidence.issues(
            principal,
            execution_id,
            IssueQuery(code=code, severity=severity, item=item, limit=limit, offset=offset),
        )
    )
    assert isinstance(result, IssueRead)
    for group in result.groups:
        typer.echo(
            f"{group.issue_code}  {group.rule_id}  {group.severity}  count: {group.count}"
        )
    if not result.groups:
        typer.echo("No issues match.")
    if code is not None or severity is not None or item is not None:
        for row in result.records:
            uom = row.selling_uom or "-"
            typer.echo(f"item: {row.store_code}/{row.item_code}/{uom}")
            typer.echo(f"  evidence: {json.dumps(row.evidence, sort_keys=True)}")
        typer.echo(
            f"records: {len(result.records)} of {result.total} "
            f"(limit {result.limit}, offset {result.offset})"
        )


@runs_app.command("report")
def runs_report(
    execution_id: UUID,
    category: Annotated[str | None, typer.Option("--category")] = None,
    item: Annotated[str | None, typer.Option("--item")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
) -> None:
    """Show the latest reconciliation revision and its exceptions."""

    evidence, principal = _evidence_service()
    result = _run(
        lambda: evidence.report(
            principal,
            execution_id,
            ReportQuery(category=category, item=item, limit=limit, offset=offset),
        )
    )
    assert isinstance(result, ReportRead)
    typer.echo(f"revision: {result.revision}")
    typer.echo(f"mode: {result.mode}")
    typer.echo(f"status: {result.status}")
    for name, count in result.counts.items():
        typer.echo(f"{name}: {count}")
    for group in result.groups:
        typer.echo(f"exception {group.category}  count: {group.count}")
    if category is not None or item is not None:
        for row in result.exceptions:
            uom = row.selling_uom or "-"
            typer.echo(f"item: {row.store_code}/{row.item_code}/{uom}  {row.category}")
            # A baseline exception is computed-versus-legacy; any other is expected-versus-actual.
            left, right = (
                ("computed", "legacy") if row.category.startswith("LEGACY_BASELINE_") else ("expected", "actual")
            )
            typer.echo(f"  {left}: {json.dumps(row.expected_evidence, sort_keys=True)}")
            typer.echo(f"  {right}: {json.dumps(row.actual_evidence, sort_keys=True)}")
        typer.echo(
            f"exceptions: {len(result.exceptions)} of {result.total} "
            f"(limit {result.limit}, offset {result.offset})"
        )


@runs_app.command("list")
def runs_list(
    workflow: Annotated[str | None, typer.Option("--workflow")] = None,
    store: Annotated[str | None, typer.Option("--store")] = None,
    started_from: Annotated[str | None, typer.Option("--from")] = None,
    started_to: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """List runs by workflow, store, or start-time range."""

    from_instant = _aware(started_from, "--from") if started_from is not None else None
    to_instant = _aware(started_to, "--to") if started_to is not None else None
    operations, principal = _service()
    query = _run(
        lambda: ExecutionQuery(
            workflow_name=workflow,
            store_code=store,
            started_from=from_instant,
            started_to=to_instant,
        )
    )
    assert isinstance(query, ExecutionQuery)
    found = _run(lambda: operations.status(principal, query))
    assert isinstance(found, list | tuple)
    for execution in found:
        typer.echo(
            f"{getattr(execution, 'id', '')}  {getattr(execution, 'workflow_name', ''):<20} "
            f"{getattr(execution, 'store_code', ''):<6} {getattr(execution, 'status', '')}"
        )
    if not found:
        typer.echo("No runs match.")


@runs_app.command("retry")
def runs_retry(execution_id: UUID, reason: Reason) -> None:
    """Create a linked retry of a FAILED run."""

    operations, principal = _service()
    _print_launch(_run(lambda: operations.retry(principal, execution_id, reason, correlation_id=uuid4())))


@runs_app.command("replay")
def runs_replay(
    execution_id: UUID, reason: Reason, window_start: WindowStart, window_end: WindowEnd
) -> None:
    """Create a linked replay of exactly one source window."""

    start = _aware(window_start, "--window-start")
    end = _aware(window_end, "--window-end")
    operations, principal = _service()
    _print_launch(
        _run(
            lambda: operations.replay(
                principal,
                execution_id,
                reason,
                correlation_id=uuid4(),
                source_window_start=start,
                source_window_end=end,
            )
        )
    )


# --- schedules and fallback -----------------------------------------------------------------

schedules_app = typer.Typer(no_args_is_help=True, help="Enable or disable a configured schedule.")


def _set_schedule(schedule_id: UUID, reason: str, *, enabled: bool) -> None:
    operations, principal = _service()
    action = operations.enable_schedule if enabled else operations.disable_schedule
    schedule = _run(lambda: action(principal, schedule_id, reason))
    state = "enabled" if getattr(schedule, "enabled", enabled) else "disabled"
    typer.echo(f"Schedule {schedule_id} is now {state}.")


@schedules_app.command("enable")
def schedules_enable(schedule_id: UUID, reason: Reason) -> None:
    """Enable one schedule (admin)."""

    _set_schedule(schedule_id, reason, enabled=True)


@schedules_app.command("disable")
def schedules_disable(schedule_id: UUID, reason: Reason) -> None:
    """Disable one schedule (admin)."""

    _set_schedule(schedule_id, reason, enabled=False)


def fallback(
    workflow: Annotated[str, typer.Option("--workflow")],
    reason: Reason,
    store: Annotated[str | None, typer.Option("--store")] = None,
) -> None:
    """Apply the in-application cutover fallback for a workflow scope (admin)."""

    operations, principal = _service()
    outcome = _run(lambda: operations.fallback(principal, reason, workflow_name=workflow, store_code=store))
    disabled = getattr(outcome, "disabled_schedule_ids", ())
    already = getattr(outcome, "already_disabled", ())
    typer.echo(f"Fallback applied at {getattr(outcome, 'applied_at', '')}.")
    typer.echo(f"disabled: {', '.join(str(i) for i in disabled) or 'none'}")
    typer.echo(f"already disabled: {', '.join(str(i) for i in already) or 'none'}")
    typer.echo("Restore the legacy trigger and reconcile the window per WORKFLOW.md Rollback / fallback.")


# --- serve -------------------------------------------------------------------------------------


def serve() -> None:
    """Run the scheduler and the internal API in the foreground (development, diagnostics)."""

    from esl_service.runtime.host import run_foreground

    try:
        run_foreground()
    except OperationsUnavailable as error:
        typer.echo(f"Unavailable: {error}")
        raise typer.Exit(code=1) from None
