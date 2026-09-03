"""Run one execution in a separate process and die after a named checkpoint.

The application-restart and partial-completion scenarios need a runner that
actually disappears mid-run, leaving committed rows and a RUNNING execution
behind for the next process to recover. ``os._exit`` skips every ``finally``
and destructor, which is the point: nothing tidies up.

Usage (from the repository root, with ESL_TEST_DATABASE_URL set)::

    python -m tests.support.run_one --execution <uuid> [--die-after canonicalize:done]
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine

from esl_service.application.runner import WorkflowRunner
from esl_service.domain.failures import RetryPolicy
from esl_service.runtime.host import RunnerPorts
from tests.support.committed import session_factory
from tests.support.sources import ScriptedSources

#: The policy the scenarios share with the runner's end-to-end tests.
POLICY = RetryPolicy(
    max_attempts=2,
    timeout_seconds=Decimal(30),
    initial_backoff_seconds=Decimal(1),
    max_backoff_seconds=Decimal(8),
    jitter_ratio=Decimal(0),
)
CLOCK = datetime(2026, 9, 2, 0, 31, tzinfo=UTC)
KILLED_EXIT_CODE = 137


class KillingPorts(RunnerPorts):
    """The real committed ports, dying right after one checkpoint is committed."""

    def __init__(self, factory: Any, die_after: str | None) -> None:
        super().__init__(factory)
        self._die_after = die_after

    def append_checkpoint(self, step_id: UUID, **fields: Any) -> Any:
        appended = super().append_checkpoint(step_id, **fields)
        if self._die_after is not None and fields.get("checkpoint_key") == self._die_after:
            sys.stdout.write(f"dying after {self._die_after}\n")
            sys.stdout.flush()
            os._exit(KILLED_EXIT_CODE)
        return appended


def build(database_url: str, die_after: str | None) -> tuple[WorkflowRunner, KillingPorts]:
    ports = KillingPorts(session_factory(create_engine(database_url)), die_after)
    runner = WorkflowRunner(
        executions=ports,
        sources=ScriptedSources(),
        retry_policy=POLICY,
        persist=ports.persist,
        clock=lambda: CLOCK,
        jitter=lambda: 0.0,
    )
    return runner, ports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", required=True, type=UUID)
    parser.add_argument("--die-after", default=None, help="checkpoint key, e.g. canonicalize:done")
    args = parser.parse_args(argv)
    runner, _ = build(os.environ["ESL_TEST_DATABASE_URL"], args.die_after)
    outcome = runner.run(args.execution)
    sys.stdout.write(f"{outcome.status.value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
