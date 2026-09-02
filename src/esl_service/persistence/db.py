"""Database engine and session-factory helpers.

The state store's password comes from the DPAPI bundle (#78, AD-017), never
from ``ESL_DATABASE_URL``. That URL names where the store is and as whom to
connect; the password is injected onto the parsed URL at engine creation, so
it never sits in the environment and never appears in a rendered URL.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from esl_service.config import Settings
from esl_service.runtime.secrets import STATE_PASSWORD_KEY, SecretProvider


def create_database_engine(database_url: str) -> Engine:
    """Create a PostgreSQL engine that checks a pooled connection before use."""

    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """Create sessions for one configured service-owned database."""

    return sessionmaker(bind=create_database_engine(database_url), expire_on_commit=False)


def create_database_engine_from_settings(settings: Settings, secrets: SecretProvider) -> Engine:
    """Create the state-store engine with its password taken from the bundle.

    A URL that still embeds a password is refused rather than merged: two
    sources of truth for one credential is how a rotation gets missed.
    """

    url = make_url(settings.database_url)
    if url.password:
        raise ValueError(
            "database_url must not embed a password; provision the "
            f"{STATE_PASSWORD_KEY} key in the secret bundle instead (AD-017)"
        )
    return create_engine(url.set(password=secrets.get(STATE_PASSWORD_KEY)), pool_pre_ping=True)


def create_session_factory_from_settings(
    settings: Settings, secrets: SecretProvider
) -> sessionmaker[Session]:
    """Create sessions for the state store using the bundled password."""

    return sessionmaker(
        bind=create_database_engine_from_settings(settings, secrets), expire_on_commit=False
    )
