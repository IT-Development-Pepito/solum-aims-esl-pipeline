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
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from esl_service.config import Settings
from esl_service.runtime.connectivity import (
    ConnectionTarget,
    Connector,
    ProbeOutcome,
    ProbeResult,
    SqlAlchemyConnector,
    parse_target,
    probe,
    state_store_target,
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

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
secrets_app = typer.Typer(no_args_is_help=True, help="Provision the DPAPI secret bundle.")
app.add_typer(secrets_app, name="secrets")

DEFAULT_BUNDLE_PATH = Path(r"C:\ProgramData\SOLUM\ESL\secrets.dpapi")

#: Exit code for a refused write, distinct from an ordinary failure.
EXIT_REFUSED = 2

# --- seams replaced by tests ----------------------------------------------

_codec: Callable[[], BundleCodec] = DpapiBundleCodec
_protector: Callable[[], FileProtector] = WindowsFileProtector
_current_sid: Callable[[], str] = current_process_sid
_connector: Callable[[], Connector] = SqlAlchemyConnector


def _record_audit(
    *, actor: str, action: str, reason: str, resource_key: str, settings: Settings | None
) -> bool:
    """Append an audit entry naming who, what, and why. Never the value.

    Best effort by design: provisioning may run before the state store is
    reachable at all, since the store's own password is provisioned this way.
    """

    if settings is None:
        return False
    try:
        from esl_service.persistence.db import create_session_factory
        from esl_service.persistence.reconciliation_repository import (
            ReconciliationRepository,
        )

        with create_session_factory(settings.database_url)() as session:
            ReconciliationRepository(session).append_audit_entry(
                actor=actor,
                action=action,
                reason=reason,
                resource_type="secret_bundle",
                resource_key=resource_key,
                outcome="APPLIED",
            )
            session.commit()
    except Exception:  # noqa: BLE001 - the state store may simply be down
        return False
    return True


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


def _audit_or_warn(*, action: str, reason: str, name: str, settings: Settings | None) -> None:
    recorded = _record_audit(
        actor=current_user_name(),
        action=action,
        reason=reason,
        resource_key=name,
        settings=settings,
    )
    if not recorded:
        typer.echo("Warning: audit entry could not be recorded; the state store is unavailable.")


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

    typer.echo(f"Stored secret '{name}' in {path}.")
    _audit_or_warn(action="secret.set", reason=reason, name=name, settings=settings)


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

    try:
        removed = _store(path, sid).remove(name)
    except SecretUnavailableError:
        typer.echo("Refused: the secret bundle is unavailable.")
        raise typer.Exit(code=1) from None
    if not removed:
        typer.echo(f"Secret '{name}' is not present in {path}.")
        raise typer.Exit(code=1)

    typer.echo(f"Removed secret '{name}' from {path}.")
    _audit_or_warn(action="secret.removed", reason=reason, name=name, settings=settings)


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

    targets: list[ConnectionTarget] = []
    if settings is not None:
        targets.append(state_store_target(settings.database_url))
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
