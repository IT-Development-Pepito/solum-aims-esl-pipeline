"""Database engine and session-factory helpers."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Create a PostgreSQL engine that checks a pooled connection before use."""

    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """Create sessions for one configured service-owned database."""

    return sessionmaker(bind=create_database_engine(database_url), expire_on_commit=False)
