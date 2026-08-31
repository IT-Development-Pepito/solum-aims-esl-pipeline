"""Dependency probe for the service-owned PostgreSQL state store (FR-024).

The state store is a required dependency: without it the service cannot record
execution state, so it must not accept new work. The probe reports only that
the store is or is not reachable — never how to reach it, because a connection
error message commonly embeds the connection string.
"""

from sqlalchemy import Engine, text

from esl_service.runtime.health import DependencyHealth, HealthState

#: Name this dependency is reported under.
STATE_STORE = "state-store"


class StateStoreProbe:
    """Checks that the service-owned PostgreSQL state store is reachable."""

    name = STATE_STORE
    required = True

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def check(self) -> DependencyHealth:
        """Return whether the state store answers a trivial query."""

        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return DependencyHealth(
            name=self.name, state=HealthState.HEALTHY, required=self.required
        )
