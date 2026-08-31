"""AIMS adapter contract tests (FR-018, FR-020).

The contracts are ports: they describe what the orchestration layer needs from
AIMS, in terms the domain already defines, without naming a transport. These
tests pin the two properties that make the boundary useful -- the port is
implementable without HTTP, and its outcomes carry everything reconciliation
(FR-021) and retry (FR-015) need.
"""

from collections.abc import Sequence
from decimal import Decimal

import pytest

from esl_service.application.contracts import (
    AimsLabel,
    AimsPageClient,
    AimsReadModelReader,
    PageChange,
    PageChangeOutcome,
    PageChangeReceipt,
)
from esl_service.domain.actions import DeliveryCertainty
from esl_service.domain.failures import (
    DependencyKind,
    FailureKind,
    FailureSignal,
    RetryPolicy,
    classify,
)
from esl_service.domain.outcomes import FailureClass

ACKNOWLEDGED = PageChangeReceipt(
    response_code="0", response_message="OK", custom_batch_id="batch-1"
)
TIMED_OUT = FailureSignal(dependency=DependencyKind.AIMS_API, kind=FailureKind.TIMEOUT)


class FakePageClient:
    """A page client that records intent and never opens a connection."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, tuple[PageChange, ...], str]] = []

    def change_pages(
        self,
        store_code: str,
        changes: Sequence[PageChange],
        idempotency_key: str,
    ) -> PageChangeOutcome:
        self.submitted.append((store_code, tuple(changes), idempotency_key))
        return PageChangeOutcome(
            certainty=DeliveryCertainty.CONFIRMED, receipt=ACKNOWLEDGED
        )


class FakeReadModelReader:
    """A read model reader backed by an in-memory list."""

    def __init__(self, labels: tuple[AimsLabel, ...]) -> None:
        self._labels = labels

    def fetch_labels(self, store_code: str) -> Sequence[AimsLabel]:
        return tuple(label for label in self._labels if label.store_code == store_code)


# --- the ports are implementable without a vendor transport (FR-018) --------


def test_page_client_port_is_satisfied_by_an_object_with_no_transport() -> None:
    """A fake with no HTTP client satisfies the port, so rules stay testable."""

    client: AimsPageClient = FakePageClient()
    outcome = client.change_pages("084", (PageChange("label-1", 3),), "key-1")

    assert outcome.certainty is DeliveryCertainty.CONFIRMED


def test_read_model_port_is_satisfied_by_an_in_memory_reader() -> None:
    """Reading labels needs no AIMS database connection to exercise."""

    reader: AimsReadModelReader = FakeReadModelReader(
        (AimsLabel("label-1", "084", 3), AimsLabel("label-2", "085", 1))
    )

    assert reader.fetch_labels("084") == (AimsLabel("label-1", "084", 3),)


def test_an_object_missing_the_page_method_is_not_a_page_client() -> None:
    """The port is checkable, so a partial adapter is rejected, not assumed."""

    assert not isinstance(object(), AimsPageClient)


# --- requests carry the identifiers the vendor call needs ------------------


@pytest.mark.parametrize("label_code", ["", "   "])
def test_page_change_rejects_a_blank_label_code(label_code: str) -> None:
    """A change without a target cannot be delivered or reconciled."""

    with pytest.raises(ValueError, match="label_code"):
        PageChange(label_code, 3)


# --- outcomes carry what retry needs (FR-015, architecture section 8) ------


def test_a_failed_outcome_carries_a_classifiable_signal() -> None:
    """Retry decisions come from the documented matrix, never from a guess."""

    outcome = PageChangeOutcome(
        certainty=DeliveryCertainty.NOT_DELIVERED, failure=TIMED_OUT
    )
    policy = RetryPolicy(
        max_attempts=3,
        timeout_seconds=Decimal(30),
        initial_backoff_seconds=Decimal(1),
        max_backoff_seconds=Decimal(60),
        jitter_ratio=Decimal("0.5"),
    )

    assert outcome.failure is not None
    assert policy.should_retry(classify(outcome.failure), attempt=1)


def test_a_failed_outcome_always_has_a_signal_to_classify() -> None:
    """Without a signal the failure could not be classified, only assumed."""

    with pytest.raises(ValueError, match="failure"):
        PageChangeOutcome(certainty=DeliveryCertainty.NOT_DELIVERED)


def test_a_confirmed_outcome_carries_the_vendor_receipt() -> None:
    """Confirmation is evidence-backed, so the batch id is recoverable."""

    outcome = PageChangeOutcome(
        certainty=DeliveryCertainty.CONFIRMED, receipt=ACKNOWLEDGED
    )

    assert outcome.receipt is not None
    assert outcome.receipt.custom_batch_id == "batch-1"


def test_a_confirmed_outcome_cannot_also_report_a_failure() -> None:
    """A single attempt has one truth; contradictory evidence is a defect."""

    with pytest.raises(ValueError):
        PageChangeOutcome(
            certainty=DeliveryCertainty.CONFIRMED,
            receipt=ACKNOWLEDGED,
            failure=TIMED_OUT,
        )


def test_a_confirmed_outcome_without_a_receipt_is_rejected() -> None:
    """Nothing may be recorded as delivered on the adapter's word alone."""

    with pytest.raises(ValueError, match="receipt"):
        PageChangeOutcome(certainty=DeliveryCertainty.CONFIRMED)


# --- outcomes carry what reconciliation needs (FR-021) --------------------


def test_an_unknown_outcome_is_expressible_rather_than_forced_to_a_verdict() -> None:
    """An interrupted call is unresolved, not silently a success or failure."""

    outcome = PageChangeOutcome(
        certainty=DeliveryCertainty.UNKNOWN,
        failure=FailureSignal(
            dependency=DependencyKind.AIMS_API, kind=FailureKind.OUTCOME_UNKNOWN
        ),
    )

    assert outcome.certainty is DeliveryCertainty.UNKNOWN


def test_outcomes_reuse_the_domain_delivery_vocabulary() -> None:
    """The port speaks the domain's terms, so no translation table is needed."""

    outcome = PageChangeOutcome(
        certainty=DeliveryCertainty.CONFIRMED, receipt=ACKNOWLEDGED
    )

    assert isinstance(outcome.certainty, DeliveryCertainty)


def test_every_failure_class_is_reachable_from_a_reported_outcome() -> None:
    """Any documented outcome can be represented, so none is silently dropped."""

    signals = [
        FailureSignal(dependency=DependencyKind.AIMS_API, kind=FailureKind.TIMEOUT),
        FailureSignal(dependency=DependencyKind.AIMS_API, kind=FailureKind.REJECTION),
        FailureSignal(
            dependency=DependencyKind.AIMS_API, kind=FailureKind.OUTCOME_UNKNOWN
        ),
    ]
    classes = {classify(signal) for signal in signals}

    assert classes == {
        FailureClass.RETRYABLE,
        FailureClass.NON_RETRYABLE,
        FailureClass.OPERATOR_ACTION_REQUIRED,
    }
