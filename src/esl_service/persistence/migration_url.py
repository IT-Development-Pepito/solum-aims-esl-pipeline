"""The URL Alembic migrates against, with its password from the bundle (AD-017).

Migrations and the service resolve the state-store credential the same way:
``ESL_DATABASE_URL`` names where and as whom, and ``state.password`` in the
secret bundle supplies the password. A URL that already embeds a password is
accepted unchanged, because the integration fixtures and CI supply the
dedicated test database that way and it is not a ``Settings`` field.

This is a separate module so it can be tested without Alembic's runtime.
"""

from collections.abc import Callable, Mapping

from sqlalchemy.engine import make_url

from esl_service.runtime.secrets import (
    STATE_PASSWORD_KEY,
    SecretProvider,
    SecretUnavailableError,
)

DEFAULT_BUNDLE_PATH = r"C:\ProgramData\SOLUM\ESL\secrets.dpapi"


def resolve_migration_url(
    environ: Mapping[str, str], secrets_factory: Callable[[str], SecretProvider]
) -> str:
    """Return the URL string Alembic should use, never logging any part of it."""

    raw = environ.get("ESL_DATABASE_URL", "")
    if not raw:
        raise RuntimeError("ESL_DATABASE_URL must be configured before running migrations")

    url = make_url(raw)
    if url.password:
        return raw

    provider = secrets_factory(environ.get("ESL_SECRET_BUNDLE_PATH", DEFAULT_BUNDLE_PATH))
    try:
        password = provider.get(STATE_PASSWORD_KEY)
    except SecretUnavailableError:
        raise RuntimeError(
            f"ESL_DATABASE_URL carries no password and the secret bundle has no "
            f"{STATE_PASSWORD_KEY} key; provision it with "
            f"`esl-admin secrets set {STATE_PASSWORD_KEY}` before migrating"
        ) from None
    return url.set(password=password).render_as_string(hide_password=False)
