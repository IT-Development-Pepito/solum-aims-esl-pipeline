"""Administrative command line: provision secrets, check connectivity (#79).

Two things an administrator needs before the service can run, both usable
without the service running. ``secrets`` writes the DPAPI bundle and is the
only supported way to provision a credential once #78 routes every password
through it. ``check-connections`` proves a credential actually works, which
setting it cannot: a secret is only shown correct by using it.

This CLI is administrative and diagnostic only. Workflow operations and the
authorization around them remain with #26 and #28.

The Windows-specific edges -- the DPAPI codec, the file protector, the process
identity, the database connector, and the audit sink -- are reached through
module-level factories so tests can replace them without touching a real
bundle, ACL, or database.
"""

import os
import secrets as secrets_module
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from esl_service.config import Settings
from esl_service.domain.authorization import parse_role_assignments
from esl_service.runtime import cli_operations
from esl_service.runtime.connectivity import (
    ConnectionTarget,
    Connector,
    ProbeOutcome,
    ProbeResult,
    SqlAlchemyConnector,
    classify_failure,
    parse_target,
    probe,
    targets_from_settings,
)
from esl_service.runtime.identity import (
    IdentityVerdict,
    check_identity,
    current_process_sid,
    current_user_name,
)
from esl_service.runtime.secrets import (
    BundleCodec,
    BundleSecretProvider,
    DpapiBundleCodec,
    FileProtector,
    InvalidSecretName,
    SecretBundleStore,
    SecretUnavailableError,
    WindowsFileProtector,
)
from esl_service.web.auth import API_TOKEN_PREFIX

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
secrets_app = typer.Typer(no_args_is_help=True, help="Provision the DPAPI secret bundle.")
app.add_typer(secrets_app, name="secrets")

# Operator commands (#28) share this entry point so one installed tool covers
# administration, diagnostics, and authorized operations (FR-029).
app.add_typer(cli_operations.runs_app, name="runs")
app.add_typer(cli_operations.schedules_app, name="schedules")
app.command("status")(cli_operations.status)
app.command("fallback")(cli_operations.fallback)
app.command("serve")(cli_operations.serve)

DEFAULT_BUNDLE_PATH = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")

#: Exit code for a refused write, distinct from an ordinary failure.
EXIT_REFUSED = 2

# --- seams replaced by tests ----------------------------------------------

_codec: Callable[[], BundleCodec] = DpapiBundleCodec
_protector: Callable[[], FileProtector] = WindowsFileProtector
_current_sid: Callable[[], str] = current_process_sid
_connector: Callable[[], Connector] = SqlAlchemyConnector


class AuditFailure(StrEnum):
    """Why an audit entry could not be recorded. Each value has a remedy."""

    NO_SETTINGS = "NO_SETTINGS"
    CONFIGURATION = "CONFIGURATION"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    SCHEMA_NOT_MIGRATED = "SCHEMA_NOT_MIGRATED"
    UNREACHABLE = "UNREACHABLE"


#: PostgreSQL undefined_table: the store answered, the tables are not there.
_UNDEFINED_TABLE = "42P01"


def classify_audit_failure(error: BaseException) -> AuditFailure:
    """Turn the exception from an audit attempt into an actionable cause.

    A store that answers but lacks the schema is the case found in use, and
    it must not read as "unavailable": the remedy is a migration, not a
    network check. The driver's text is never used, only its SQLSTATE.
    """

    if isinstance(error, SecretUnavailableError):
        return AuditFailure.SECRET_UNAVAILABLE
    if isinstance(error, ValueError):
        return AuditFailure.CONFIGURATION

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        if getattr(current, "sqlstate", None) == _UNDEFINED_TABLE:
            return AuditFailure.SCHEMA_NOT_MIGRATED
        current = getattr(current, "orig", None) or current.__cause__

    if classify_failure(error) is ProbeOutcome.CREDENTIAL_REJECTED:
        return AuditFailure.CREDENTIAL_REJECTED
    return AuditFailure.UNREACHABLE


def _record_audit(
    *,
    actor: str,
    action: str,
    reason: str,
    resource_key: str,
    settings: Settings | None,
    bundle: Path,
    after_evidence: Mapping[str, bool] | None = None,
) -> AuditFailure | None:
    """Append an audit entry naming who, what, and why. Never the value.

    Best effort by design: provisioning may run before the state store is
    reachable at all, since the store's own password is provisioned this way.
    Returns None when recorded, otherwise the classified cause.
    """

    if settings is None:
        return AuditFailure.NO_SETTINGS
    try:
        from esl_service.persistence.db import create_session_factory_from_settings
        from esl_service.persistence.reconciliation_repository import (
            ReconciliationRepository,
        )

        factory = create_session_factory_from_settings(
            settings, BundleSecretProvider(bundle, _codec())
        )
        with factory() as session:
            ReconciliationRepository(session).append_audit_entry(
                actor=actor,
                action=action,
                reason=reason,
                resource_type="secret_bundle",
                resource_key=resource_key,
                outcome="APPLIED",
                after_evidence=after_evidence,
            )
            session.commit()
    except Exception as error:  # noqa: BLE001 - classified, never echoed
        return classify_audit_failure(error)
    return None


# --- shared helpers --------------------------------------------------------


def _load_settings() -> Settings | None:
    try:
        return Settings.model_validate({})
    except ValidationError:
        return None


def _bundle_path(explicit: Path | None, settings: Settings | None) -> Path:
    if explicit is not None:
        return explicit
    if settings is not None:
        return settings.secret_bundle_path
    return Path(os.environ.get("ESL_SECRET_BUNDLE_PATH", str(DEFAULT_BUNDLE_PATH)))


def _expected_sid(settings: Settings | None) -> str:
    if settings is not None:
        return settings.service_identity_sid
    return os.environ.get("ESL_SERVICE_IDENTITY_SID", "")


def _guard_identity(settings: Settings | None) -> str | None:
    """Refuse a write as the wrong account; warn when no account is configured.

    Returns the SID the bundle should be protected for, or None when the
    development machine has no service account.
    """

    expected = _expected_sid(settings)
    verdict = check_identity(current_sid=_current_sid(), expected_sid=expected)
    if verdict is IdentityVerdict.MISMATCH:
        typer.echo(
            "Refused: this process is not running as the configured service account. "
            "Under user-scope DPAPI a bundle written by another account cannot be read "
            "by the service. Run this command as the service account."
        )
        raise typer.Exit(code=EXIT_REFUSED)
    if verdict is IdentityVerdict.UNCONFIGURED:
        typer.echo("Identity check skipped: no service identity is configured.")
        return None
    return expected.strip().upper()


def _store(bundle: Path, sid: str | None) -> SecretBundleStore:
    return SecretBundleStore(
        bundle, codec=_codec(), protector=_protector(), service_identity_sid=sid
    )


def _ensure_bundle_directory(bundle: Path, sid: str | None) -> None:
    """Make sure the bundle's directory exists, or refuse in a controlled way.

    On a development machine, where no service identity is configured, the
    directory is created and the creation announced. With a service identity
    configured it is never created here: the startup validator checks the
    directory's owner and ACL, so a folder made with inherited permissions
    would be accepted by this tool and rejected by the service.
    """

    directory = bundle.parent
    if directory.is_dir():
        return
    if sid is None:
        directory.mkdir(parents=True, exist_ok=True)
        typer.echo(
            f"Created bundle directory {directory} (development). In staging and "
            "production create it first, as an administrator, with an ACL limited to "
            "the service account, Administrators, and SYSTEM."
        )
        return
    typer.echo(
        f"Refused: the bundle directory {directory} does not exist. Create it as an "
        "administrator with an ACL limited to the service account, Administrators, "
        "and SYSTEM, then run this command again."
    )
    raise typer.Exit(code=1)


def _refuse_filesystem(path: Path) -> None:
    """Report a filesystem refusal without the driver text, and exit."""

    typer.echo(
        f"Refused: the filesystem denied access to {path} (a permission or path "
        "problem). No secret was stored. Check the directory's ACL and that this "
        "command runs as the account that owns the bundle."
    )
    raise typer.Exit(code=1)


#: One remedy per cause. A warning that cannot be acted on is not a warning.
_AUDIT_REMEDY = {
    AuditFailure.NO_SETTINGS: (
        "the configuration could not be loaded, so no state store is known. "
        "Set the ESL_* variables, or ignore this on a machine that only provisions."
    ),
    AuditFailure.CONFIGURATION: (
        "ESL_DATABASE_URL still embeds a password. Remove it and provision "
        "state.password in the bundle instead (AD-017)."
    ),
    AuditFailure.SECRET_UNAVAILABLE: (
        "the bundle has no state.password key yet. Run "
        "`esl-admin secrets set state.password` first."
    ),
    AuditFailure.CREDENTIAL_REJECTED: (
        "the state store refused the state.password value in the bundle. "
        "Set it again with the account's current password."
    ),
    AuditFailure.SCHEMA_NOT_MIGRATED: (
        "the state store answered but its schema is not migrated. Run "
        "`alembic upgrade head` against ESL_DATABASE_URL."
    ),
    AuditFailure.UNREACHABLE: (
        "could not reach the state store named in ESL_DATABASE_URL. "
        "Check the host, port, and database with `esl-admin check-connections`."
    ),
}


def _audit_or_warn(
    *,
    action: str,
    reason: str,
    name: str,
    settings: Settings | None,
    bundle: Path,
    after_evidence: Mapping[str, bool] | None = None,
) -> None:
    failure = _record_audit(
        actor=current_user_name(),
        action=action,
        reason=reason,
        resource_key=name,
        settings=settings,
        bundle=bundle,
        after_evidence=after_evidence,
    )
    if failure is not None:
        typer.echo(
            f"Warning: audit entry could not be recorded: {_AUDIT_REMEDY[failure]} "
            "The secret itself was stored."
        )


BundleOption = Annotated[
    Path | None,
    typer.Option("--bundle", help="Bundle path; defaults to the configured secret_bundle_path."),
]
ReasonOption = Annotated[str, typer.Option("--reason", help="Why, e.g. a change ticket.")]


# --- secrets ---------------------------------------------------------------


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Bundle key, e.g. aims.portal.password.")],
    reason: ReasonOption,
    bundle: BundleOption = None,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the value from standard input.")
    ] = False,
) -> None:
    """Store or replace one secret. The value is never taken as an argument."""

    settings = _load_settings()
    sid = _guard_identity(settings)

    if stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        value = typer.prompt("Secret value", hide_input=True, confirmation_prompt=True)

    path = _bundle_path(bundle, settings)
    _ensure_bundle_directory(path, sid)
    try:
        _store(path, sid).set(name, value)
    except InvalidSecretName as error:
        typer.echo(f"Refused: {error}")
        raise typer.Exit(code=1) from None
    except SecretUnavailableError:
        typer.echo("Refused: the existing secret bundle is unavailable and will not be overwritten.")
        raise typer.Exit(code=1) from None
    except ValueError as error:
        typer.echo(f"Refused: {error}")
        raise typer.Exit(code=1) from None
    except OSError:
        # FileNotFoundError and PermissionError included: the OS text can name
        # paths and accounts, so a fixed message replaces it.
        _refuse_filesystem(path)

    typer.echo(f"Stored secret '{name}' in {path}.")
    _audit_or_warn(
        action="secret.set", reason=reason, name=name, settings=settings, bundle=path
    )


@secrets_app.command("issue-token")
def secrets_issue_token(
    account: Annotated[str, typer.Argument(help="Account the token authenticates, e.g. ops.alice.")],
    reason: ReasonOption,
    bundle: BundleOption = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the token to this file, protected by the bundle's ACL."),
    ] = None,
    stdout: Annotated[
        bool, typer.Option("--stdout", help="Print the token to standard output instead.")
    ] = False,
) -> None:
    """Generate, store, and reveal one API token; run again to rotate it (#98).

    The value is generated here rather than pasted, so a scripted environment
    setup never carries a secret through a clipboard or a half-typed prompt.
    It is revealed exactly once, through the one channel the caller names, and
    it never reaches the audit entry or any log.
    """

    if out is not None and stdout:
        typer.echo("Refused: name one reveal channel, --out <path> or --stdout, not both.")
        raise typer.Exit(code=2)
    if out is None and not stdout:
        # A console operator is a reveal channel and can just run the command.
        # A pipe, a redirect, or a transcript is not one anyone chose, so the
        # token is not written there by default.
        if not _stdout_is_a_terminal():
            typer.echo(
                "Refused: standard output is not a terminal, so name a reveal channel, "
                "--out <path> or --stdout."
            )
            raise typer.Exit(code=2)
        stdout = True

    settings = _load_settings()
    sid = _guard_identity(settings)
    name = f"{API_TOKEN_PREFIX}{account}"
    if out is not None and out.exists():
        # Never overwrite: the existing file may hold a token still in use.
        typer.echo(f"Refused: {out} exists. Remove it first, or name another path.")
        raise typer.Exit(code=1)

    token = secrets_module.token_urlsafe(32)
    path = _bundle_path(bundle, settings)
    _ensure_bundle_directory(path, sid)
    store = _store(path, sid)
    try:
        # keys() is the store's own listing, not a mapping view.
        existing = store.keys()
    except SecretUnavailableError:
        existing = ()
    rotated = name in existing
    try:
        store.set(name, token)
    except InvalidSecretName:
        typer.echo(
            f"Refused: '{account}' is not a usable account name; it must be letters, "
            "digits, dots, dashes, or underscores."
        )
        raise typer.Exit(code=1) from None
    except SecretUnavailableError:
        typer.echo("Refused: the existing secret bundle is unavailable and will not be overwritten.")
        raise typer.Exit(code=1) from None
    except OSError:
        _refuse_filesystem(path)

    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(token + "\n", encoding="utf-8")
            _protector().protect(out, sid)
        except OSError:
            _refuse_filesystem(out)
        typer.echo(f"Issued a token for '{account}' and wrote it to {out}.")
    else:
        typer.echo(f"Issued a token for '{account}'. It is shown once and not stored elsewhere:")
        typer.echo(token)

    _warn_when_account_has_no_role(account, settings)
    # Both runs are the same action on the same key, so only the evidence tells
    # the first provisioning of an account apart from a rotation that
    # invalidated a token already in use.
    _audit_or_warn(
        action="secret.set",
        reason=reason,
        name=name,
        settings=settings,
        bundle=path,
        after_evidence={"rotated": rotated},
    )
    if rotated:
        typer.echo(
            f"The previous token for '{account}' no longer authenticates. Restart the "
            "service so it reloads the bundle."
        )


def _stdout_is_a_terminal() -> bool:
    """Seam: a test drives both answers without owning a console."""

    return sys.stdout.isatty()


def _warn_when_account_has_no_role(account: str, settings: Settings | None) -> None:
    """A token that authenticates and is then refused is a support call, not a setup.

    The assignment text is read from settings when they load, and from the
    environment when they do not: on a development machine the rest of the
    configuration is often absent, and the warning is still worth having.
    """

    configured = (
        settings.operator_roles
        if settings is not None
        else os.environ.get("ESL_OPERATOR_ROLES", "")
    )
    try:
        assignments = parse_role_assignments(configured)
    except ValueError:
        # Malformed assignments stop the service at startup; that is where the
        # operator should see it, not here.
        return
    if account in assignments:
        return
    typer.echo(
        f"Warning: '{account}' holds no role in ESL_OPERATOR_ROLES, so this token will "
        "authenticate and then be refused. Assign a role before the account is used."
    )


@secrets_app.command("remove")
def secrets_remove(
    name: Annotated[str, typer.Argument()],
    reason: ReasonOption,
    bundle: BundleOption = None,
) -> None:
    """Remove one secret from the bundle."""

    settings = _load_settings()
    sid = _guard_identity(settings)
    path = _bundle_path(bundle, settings)
    _ensure_bundle_directory(path, sid)

    try:
        removed = _store(path, sid).remove(name)
    except SecretUnavailableError:
        typer.echo("Refused: the secret bundle is unavailable.")
        raise typer.Exit(code=1) from None
    except OSError:
        _refuse_filesystem(path)
    if not removed:
        typer.echo(f"Secret '{name}' is not present in {path}.")
        raise typer.Exit(code=1)

    typer.echo(f"Removed secret '{name}' from {path}.")
    _audit_or_warn(
        action="secret.removed", reason=reason, name=name, settings=settings, bundle=path
    )


@secrets_app.command("list")
def secrets_list(bundle: BundleOption = None) -> None:
    """List the names the bundle holds. Values are never shown."""

    path = _bundle_path(bundle, _load_settings())
    try:
        names = _store(path, None).keys()
    except SecretUnavailableError:
        typer.echo(f"Secret bundle is unavailable at {path}.")
        raise typer.Exit(code=1) from None

    if not names:
        typer.echo(f"Secret bundle at {path} holds no secrets.")
        return
    for name in names:
        typer.echo(name)


# --- check-connections -----------------------------------------------------


@app.command("check-connections")
def check_connections(
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target",
            help="name=kind://user@host:port/db#password.key; repeatable.",
        ),
    ] = None,
    bundle: BundleOption = None,
) -> None:
    """Attempt every configured database and report reachability, never how."""

    settings = _load_settings()
    path = _bundle_path(bundle, settings)

    # Every tier configuration names, configured or not, so a gap is listed
    # rather than silently absent (#78). --target adds ad-hoc extras.
    targets: list[ConnectionTarget] = (
        list(targets_from_settings(settings)) if settings is not None else []
    )
    for spec in target or []:
        try:
            targets.append(parse_target(spec))
        except ValueError as error:
            typer.echo(f"Invalid --target: {error}")
            raise typer.Exit(code=1) from None

    if not targets:
        _print_row(
            ProbeResult(
                "(none)", ProbeOutcome.UNCONFIGURED, detail="no database target is configured"
            )
        )
        return

    secrets = BundleSecretProvider(path, _codec())
    connector = _connector()
    results = [probe(item, secrets, connector) for item in targets]
    for result in results:
        _print_row(result)

    if not all(result.ok for result in results):
        raise typer.Exit(code=1)


def _print_row(result: ProbeResult) -> None:
    identity = result.identity or "-"
    detail = f"  {result.detail}" if result.detail else ""
    typer.echo(f"{result.name:<20} {result.outcome.value:<20} {identity}{detail}")
