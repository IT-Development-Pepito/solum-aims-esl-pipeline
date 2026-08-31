"""Failure classification and bounded retry policy (FR-014, FR-015).

The classification matrix is exactly the one documented in
``docs/SYSTEM_ARCHITECTURE.md`` section 8. Nothing is inferred: an
unrecognised dependency and failure combination raises rather than defaulting,
because a wrong default would either retry something unsafe or silently
abandon something recoverable.
"""

import os
from decimal import Decimal

import pytest

from esl_service.config import Settings
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    RetryPolicy,
    UnclassifiedFailure,
    classify,
)
from esl_service.domain.outcomes import FailureClass


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient ESL_ variables so these unit tests are deterministic."""

    for name in [key for key in os.environ if key.startswith("ESL_")]:
        monkeypatch.delenv(name, raising=False)


def policy(**overrides: object) -> RetryPolicy:
    """Build a retry policy, overriding only what a test needs."""

    values: dict[str, object] = {
        "max_attempts": 3,
        "timeout_seconds": Decimal(30),
        "initial_backoff_seconds": Decimal(1),
        "max_backoff_seconds": Decimal(60),
        "jitter_ratio": Decimal("0.5"),
    }
    values.update(overrides)
    return RetryPolicy(**values)  # type: ignore[arg-type]


# --- the documented matrix (architecture section 8) -------------------------


@pytest.mark.parametrize(
    ("dependency", "kind", "expected"),
    [
        # "Retryable within policy; retain window/checkpoint."
        (DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE, FailureClass.RETRYABLE),
        (DependencyKind.SQL_SERVER, FailureKind.TIMEOUT, FailureClass.RETRYABLE),
        # "Retryable only with idempotency key."
        (DependencyKind.AIMS_API, FailureKind.UNAVAILABLE, FailureClass.RETRYABLE),
        (
            DependencyKind.AIMS_API,
            FailureKind.NETWORK_INTERRUPTION,
            FailureClass.RETRYABLE,
        ),
        # "Usually non-retryable until corrected."
        (DependencyKind.AIMS_API, FailureKind.REJECTION, FailureClass.NON_RETRYABLE),
        (
            DependencyKind.AIMS_API,
            FailureKind.UNEXPECTED_RESPONSE,
            FailureClass.NON_RETRYABLE,
        ),
        # "Retry only availability errors."
        (
            DependencyKind.AIMS_COMPATIBILITY,
            FailureKind.UNAVAILABLE,
            FailureClass.RETRYABLE,
        ),
        (
            DependencyKind.AIMS_COMPATIBILITY,
            FailureKind.SCHEMA_DRIFT,
            FailureClass.NON_RETRYABLE,
        ),
        # "Non-retryable until corrected."
        (DependencyKind.SOURCE_DATA, FailureKind.MALFORMED, FailureClass.NON_RETRYABLE),
        (
            DependencyKind.CONFIGURATION,
            FailureKind.MALFORMED,
            FailureClass.NON_RETRYABLE,
        ),
        # "Non-retryable until a correction or approved policy exists."
        (
            DependencyKind.PROMOTION_RULES,
            FailureKind.AMBIGUOUS,
            FailureClass.NON_RETRYABLE,
        ),
        (
            DependencyKind.PROMOTION_RULES,
            FailureKind.UNSUPPORTED_UOM,
            FailureClass.NON_RETRYABLE,
        ),
        # "Stop new work safely when durability/audit cannot be ensured."
        (
            DependencyKind.HOST,
            FailureKind.CAPACITY_EXHAUSTED,
            FailureClass.OPERATOR_ACTION_REQUIRED,
        ),
        # "Retry after approved rotation only."
        (
            DependencyKind.CREDENTIAL,
            FailureKind.EXPIRED,
            FailureClass.OPERATOR_ACTION_REQUIRED,
        ),
        # Architecture 5.6: OUTCOME_UNKNOWN is operator-action-required.
        (
            DependencyKind.AIMS_API,
            FailureKind.OUTCOME_UNKNOWN,
            FailureClass.OPERATOR_ACTION_REQUIRED,
        ),
    ],
)
def test_documented_matrix_rows(
    dependency: DependencyKind, kind: FailureKind, expected: FailureClass
) -> None:
    """Every row classifies exactly as architecture section 8 states."""

    assert classify(FailureSignal(dependency, kind)) is expected


def test_an_unknown_combination_raises_rather_than_defaulting() -> None:
    """A wrong default would retry something unsafe or abandon something safe."""

    with pytest.raises(UnclassifiedFailure, match="CREDENTIAL"):
        classify(FailureSignal(DependencyKind.CREDENTIAL, FailureKind.MALFORMED))


def test_malformed_data_is_never_retryable() -> None:
    """Retrying malformed data is an explicit non-goal of this issue."""

    for dependency in (DependencyKind.SOURCE_DATA, DependencyKind.CONFIGURATION):
        assert (
            classify(FailureSignal(dependency, FailureKind.MALFORMED))
            is not FailureClass.RETRYABLE
        )


def test_an_unknown_external_outcome_is_never_retryable() -> None:
    """An unverified external submission is never resent automatically."""

    signal = FailureSignal(DependencyKind.AIMS_API, FailureKind.OUTCOME_UNKNOWN)
    assert classify(signal) is FailureClass.OPERATOR_ACTION_REQUIRED
    assert policy().should_retry(classify(signal), attempt=1) is False


# --- bounded retry (FR-015) -------------------------------------------------


def test_only_retryable_failures_are_retried() -> None:
    """Classification, not optimism, decides whether to try again."""

    assert policy().should_retry(FailureClass.RETRYABLE, attempt=1) is True
    assert policy().should_retry(FailureClass.NON_RETRYABLE, attempt=1) is False
    assert (
        policy().should_retry(FailureClass.OPERATOR_ACTION_REQUIRED, attempt=1)
        is False
    )


def test_retry_stops_at_the_configured_limit() -> None:
    """No retry beyond the configured attempt limit (FR-015)."""

    configured = policy(max_attempts=3)

    assert configured.should_retry(FailureClass.RETRYABLE, attempt=1) is True
    assert configured.should_retry(FailureClass.RETRYABLE, attempt=2) is True
    assert configured.should_retry(FailureClass.RETRYABLE, attempt=3) is False


def test_backoff_grows_exponentially_and_is_bounded() -> None:
    """Delay doubles per attempt and never exceeds the configured maximum."""

    configured = policy(
        initial_backoff_seconds=Decimal(1),
        max_backoff_seconds=Decimal(8),
        jitter_ratio=Decimal(0),
    )
    delays = [configured.delay_for(attempt, jitter=1.0) for attempt in range(1, 7)]

    assert delays[:4] == [Decimal(1), Decimal(2), Decimal(4), Decimal(8)]
    assert all(delay <= Decimal(8) for delay in delays)


def test_jitter_only_reduces_the_delay_and_never_below_zero() -> None:
    """Jitter spreads retries without ever exceeding the bounded delay."""

    configured = policy(
        initial_backoff_seconds=Decimal(10),
        max_backoff_seconds=Decimal(10),
        jitter_ratio=Decimal("0.5"),
    )

    assert configured.delay_for(1, jitter=1.0) == Decimal(10)
    assert configured.delay_for(1, jitter=0.0) == Decimal(5)
    assert Decimal(5) <= configured.delay_for(1, jitter=0.5) <= Decimal(10)


def test_jitter_fraction_must_be_a_proportion() -> None:
    """A jitter source outside zero to one is a programming error."""

    for jitter in (-0.1, 1.1):
        with pytest.raises(ValueError, match="jitter"):
            policy().delay_for(1, jitter=jitter)


def test_attempt_must_start_at_one() -> None:
    """Attempts are numbered from one, matching the action attempt ledger."""

    with pytest.raises(ValueError, match="attempt"):
        policy().delay_for(0, jitter=0.5)


# --- externalised, audit-visible configuration (FR-015, FR-025) -------------


def settings(**overrides: object) -> Settings:
    """Build development settings, overriding only what a test needs."""

    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://user:pw@localhost/esl",
        "internal_host": "127.0.0.1",
        "shadow_mode": True,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_policy_is_built_from_externalised_configuration() -> None:
    """Retry behaviour is configuration, not a code constant (FR-015)."""

    configured = RetryPolicy.from_settings(
        settings(retry_max_attempts=5, retry_timeout_seconds=Decimal(45))
    )
    assert configured.max_attempts == 5
    assert configured.timeout_seconds == Decimal(45)


def test_retry_configuration_is_audit_visible() -> None:
    """The configuration version records the retry policy it ran under."""

    from esl_service.config import sanitized_configuration_snapshot

    snapshot = sanitized_configuration_snapshot(settings())
    for key in (
        "retry_max_attempts",
        "retry_timeout_seconds",
        "retry_initial_backoff_seconds",
        "retry_max_backoff_seconds",
        "retry_jitter_ratio",
    ):
        assert key in snapshot


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retry_max_attempts", 0),
        ("retry_timeout_seconds", Decimal(0)),
        ("retry_initial_backoff_seconds", Decimal(0)),
        ("retry_max_backoff_seconds", Decimal(0)),
    ],
)
def test_non_positive_retry_configuration_is_refused(
    field: str, value: object
) -> None:
    """A zero limit or delay is never a valid retry configuration."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=field):
        settings(**{field: value})


def test_backoff_maximum_must_not_precede_the_initial_delay() -> None:
    """An upper bound below the first delay is a contradictory configuration."""

    with pytest.raises(ValueError, match="max_backoff_seconds"):
        policy(
            initial_backoff_seconds=Decimal(10), max_backoff_seconds=Decimal(1)
        )


def test_jitter_ratio_must_be_a_proportion() -> None:
    """The configured jitter ratio is a proportion of the bounded delay."""

    with pytest.raises(ValueError, match="jitter_ratio"):
        policy(jitter_ratio=Decimal("1.5"))
