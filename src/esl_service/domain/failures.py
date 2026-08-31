"""Dependency failure classification and bounded retry (FR-014, FR-015).

The classification matrix below is exactly the one documented in
``docs/SYSTEM_ARCHITECTURE.md`` section 8, one entry per documented row. It is
transcribed, not inferred, and each entry cites the row it came from.

An unrecognised dependency and failure combination **raises** rather than
defaulting. A default of retryable would resend something unsafe; a default of
non-retryable would silently abandon recoverable work. Both are worse than a
loud failure that names the gap.

Two rules from elsewhere are honoured here:

* an unknown external outcome is operator-action-required and is never resent
  automatically (architecture 5.6, FR-013);
* malformed data is never retried, which is an explicit non-goal of #20.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from esl_service.domain.outcomes import FailureClass


class DependencyKind(StrEnum):
    """A dependency the service can fail against (architecture section 8)."""

    SQL_SERVER = "SQL_SERVER"
    AIMS_API = "AIMS_API"
    AIMS_COMPATIBILITY = "AIMS_COMPATIBILITY"
    SOURCE_DATA = "SOURCE_DATA"
    CONFIGURATION = "CONFIGURATION"
    PROMOTION_RULES = "PROMOTION_RULES"
    HOST = "HOST"
    CREDENTIAL = "CREDENTIAL"


class FailureKind(StrEnum):
    """How a dependency failed (architecture section 8)."""

    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    NETWORK_INTERRUPTION = "NETWORK_INTERRUPTION"
    REJECTION = "REJECTION"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    MALFORMED = "MALFORMED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED_UOM = "UNSUPPORTED_UOM"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    EXPIRED = "EXPIRED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class FailureSignal:
    """One observed dependency failure, before it is classified."""

    dependency: DependencyKind
    kind: FailureKind


class UnclassifiedFailure(LookupError):
    """Raised when no documented matrix row covers an observed failure."""


#: Transcribed from architecture section 8. Each entry names its source row.
_MATRIX: dict[tuple[DependencyKind, FailureKind], FailureClass] = {
    # "SQL Server unavailable/timeout ... Retryable within policy."
    (DependencyKind.SQL_SERVER, FailureKind.UNAVAILABLE): FailureClass.RETRYABLE,
    (DependencyKind.SQL_SERVER, FailureKind.TIMEOUT): FailureClass.RETRYABLE,
    # "AIMS/API unavailable/network interruption ... Retryable only with
    # idempotency key." Every action carries one by construction (#19).
    (DependencyKind.AIMS_API, FailureKind.UNAVAILABLE): FailureClass.RETRYABLE,
    (DependencyKind.AIMS_API, FailureKind.TIMEOUT): FailureClass.RETRYABLE,
    (
        DependencyKind.AIMS_API,
        FailureKind.NETWORK_INTERRUPTION,
    ): FailureClass.RETRYABLE,
    # "AIMS rejection/unexpected response ... Usually non-retryable until
    # corrected."
    (DependencyKind.AIMS_API, FailureKind.REJECTION): FailureClass.NON_RETRYABLE,
    (
        DependencyKind.AIMS_API,
        FailureKind.UNEXPECTED_RESPONSE,
    ): FailureClass.NON_RETRYABLE,
    # Architecture 5.6: OUTCOME_UNKNOWN is operator-action-required and blocks
    # blind resubmission.
    (
        DependencyKind.AIMS_API,
        FailureKind.OUTCOME_UNKNOWN,
    ): FailureClass.OPERATOR_ACTION_REQUIRED,
    # "Compatibility DB unavailable/schema drift ... Retry only availability
    # errors."
    (
        DependencyKind.AIMS_COMPATIBILITY,
        FailureKind.UNAVAILABLE,
    ): FailureClass.RETRYABLE,
    (
        DependencyKind.AIMS_COMPATIBILITY,
        FailureKind.SCHEMA_DRIFT,
    ): FailureClass.NON_RETRYABLE,
    # "Malformed source/configuration ... Non-retryable until corrected."
    (DependencyKind.SOURCE_DATA, FailureKind.MALFORMED): FailureClass.NON_RETRYABLE,
    (DependencyKind.CONFIGURATION, FailureKind.MALFORMED): FailureClass.NON_RETRYABLE,
    # "Promotion ambiguity or unsupported UOM ... Non-retryable until a
    # correction or approved policy exists."
    (DependencyKind.PROMOTION_RULES, FailureKind.AMBIGUOUS): FailureClass.NON_RETRYABLE,
    (
        DependencyKind.PROMOTION_RULES,
        FailureKind.UNSUPPORTED_UOM,
    ): FailureClass.NON_RETRYABLE,
    # "Disk exhaustion / logging outage ... Stop new work safely when
    # durability/audit cannot be ensured."
    (
        DependencyKind.HOST,
        FailureKind.CAPACITY_EXHAUSTED,
    ): FailureClass.OPERATOR_ACTION_REQUIRED,
    # "Expired/rotated credential ... Retry after approved rotation only."
    (
        DependencyKind.CREDENTIAL,
        FailureKind.EXPIRED,
    ): FailureClass.OPERATOR_ACTION_REQUIRED,
}


def classify(signal: FailureSignal) -> FailureClass:
    """Classify one observed failure using the documented matrix (FR-014)."""

    try:
        return _MATRIX[(signal.dependency, signal.kind)]
    except KeyError:
        raise UnclassifiedFailure(
            f"no documented failure row for {signal.dependency.value} "
            f"and {signal.kind.value}; architecture section 8 must define it "
            "before it can be handled"
        ) from None


@dataclass(frozen=True)
class RetryPolicy:
    """Configured retry limit, timeout, and bounded backoff (FR-015)."""

    max_attempts: int
    timeout_seconds: Decimal
    initial_backoff_seconds: Decimal
    max_backoff_seconds: Decimal
    jitter_ratio: Decimal

    def __post_init__(self) -> None:
        for name in (
            "max_attempts",
            "timeout_seconds",
            "initial_backoff_seconds",
            "max_backoff_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not Decimal(0) <= self.jitter_ratio <= Decimal(1):
            raise ValueError("jitter_ratio must be between zero and one")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must not be below initial_backoff_seconds"
            )

    def should_retry(self, failure_class: FailureClass, attempt: int) -> bool:
        """Return whether another attempt is permitted.

        Only a retryable classification is ever retried, and never beyond the
        configured limit.
        """

        if failure_class is not FailureClass.RETRYABLE:
            return False
        return attempt < self.max_attempts

    def delay_for(self, attempt: int, *, jitter: float) -> Decimal:
        """Return the bounded, jittered delay before one attempt.

        The delay doubles per attempt up to the configured maximum. Jitter
        reduces it by up to ``jitter_ratio`` so simultaneous failures do not
        retry in lockstep; it never extends the delay past the bound.

        ``jitter`` is supplied by the caller rather than drawn here, so the
        delay is deterministic and testable.
        """

        if attempt < 1:
            raise ValueError("attempt must start at 1")
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be a proportion between zero and one")

        # Decimal(2) rather than 2, so the exponentiation stays exact Decimal
        # arithmetic instead of widening through int.__pow__.
        growth = Decimal(2) ** (attempt - 1)
        base = min(self.initial_backoff_seconds * growth, self.max_backoff_seconds)
        reduction = self.jitter_ratio * (Decimal(1) - Decimal(str(jitter)))
        return base * (Decimal(1) - reduction)
