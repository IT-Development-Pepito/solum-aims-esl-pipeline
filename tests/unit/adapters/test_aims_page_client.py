"""Contract tests for the AIMS page-change adapter (#23, AD-021).

The contract of record is the deployed Dashboard operation
``POST /dashboardservice/common/labels/page?store=<code>`` with a
``pageChangeList`` of ``labelCode``/``page`` entries, evidenced by the three
Hop ``.hpl`` REST steps that have posted to it in production and by the
runbook section 13.6; the response fields ``responseCode``,
``responseMessage``, and ``customBatchId`` are the VERIFIED reading of the
deployed OpenAPI recorded in ``docs/SPECIFICATION.md``.

Every case below fixes one row of the architecture section 8 matrix, and the
split that matters most is between "the request never arrived" and "the
request may have been applied and we have no answer". The first is
retryable; the second is never resent automatically (FR-013).
"""

import httpx
import pytest
import respx

from esl_service.adapters.aims_page import AimsPageClientHttp, PageChangeRequestError
from esl_service.application.contracts import AimsPageClient, PageChange
from esl_service.domain.actions import DeliveryCertainty
from esl_service.domain.failures import (
    DependencyKind,
    FailureClass,
    FailureKind,
    classify,
)

BASE_URL = "http://aims.internal:9001/dashboardservice"
PAGE_URL = f"{BASE_URL}/common/labels/page"
STORE = "084"
KEY = "k" * 64
CHANGES = (PageChange("LBL-0001", 2), PageChange("LBL-0002", 1))


def client(**overrides: object) -> AimsPageClientHttp:
    return AimsPageClientHttp(base_url=BASE_URL, timeout_seconds=2, **overrides)  # type: ignore[arg-type]


def accepted(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "responseCode": "200",
        "responseMessage": "SUCCESS",
        "customBatchId": "batch-42",
    }
    body.update(overrides)
    return body


# --- the request the vendor documents -------------------------------------------


@respx.mock
def test_the_request_matches_the_operation_of_record() -> None:
    route = respx.post(PAGE_URL).mock(return_value=httpx.Response(200, json=accepted()))

    client().change_pages(STORE, CHANGES, KEY)

    request = route.calls.last.request
    assert request.method == "POST"
    assert request.url.path == "/dashboardservice/common/labels/page"
    assert dict(request.url.params) == {"store": STORE}
    assert request.headers["content-type"].startswith("application/json")
    import json

    assert json.loads(request.content) == {
        "pageChangeList": [
            {"labelCode": "LBL-0001", "page": 2},
            {"labelCode": "LBL-0002", "page": 1},
        ]
    }


@respx.mock
def test_every_submission_carries_its_idempotency_key() -> None:
    """The vendor documents no idempotency support, so the key is ours to send and log."""

    route = respx.post(PAGE_URL).mock(return_value=httpx.Response(200, json=accepted()))

    client().change_pages(STORE, CHANGES, KEY)

    assert route.calls.last.request.headers["Idempotency-Key"] == KEY


def test_an_empty_batch_never_reaches_the_vendor() -> None:
    with pytest.raises(PageChangeRequestError, match="at least one page change"):
        client().change_pages(STORE, (), KEY)


def test_the_adapter_satisfies_the_application_port() -> None:
    assert isinstance(client(), AimsPageClient)


# --- confirmed ------------------------------------------------------------------


@respx.mock
def test_an_accepted_batch_is_confirmed_with_the_vendor_receipt() -> None:
    respx.post(PAGE_URL).mock(return_value=httpx.Response(200, json=accepted()))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.CONFIRMED
    assert outcome.failure is None
    assert outcome.receipt is not None
    assert outcome.receipt.response_code == "200"
    assert outcome.receipt.response_message == "SUCCESS"
    assert outcome.receipt.custom_batch_id == "batch-42"


@respx.mock
def test_a_missing_batch_id_is_still_a_confirmation() -> None:
    """``customBatchId`` is the one field resting on a transcribed reading (AD-021)."""

    respx.post(PAGE_URL).mock(
        return_value=httpx.Response(200, json={"responseCode": "200", "responseMessage": "OK"})
    )

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.CONFIRMED
    assert outcome.receipt is not None and outcome.receipt.custom_batch_id is None


# --- the vendor answered and refused --------------------------------------------


@respx.mock
@pytest.mark.parametrize("body", [
    {"responseCode": "400", "responseMessage": "INVALID LABEL"},
    {"responseCode": "E001", "responseMessage": "unknown store"},
])
def test_a_refusal_the_vendor_states_is_non_retryable(body: dict[str, str]) -> None:
    respx.post(PAGE_URL).mock(return_value=httpx.Response(200, json=body))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.NOT_DELIVERED
    assert outcome.failure == _signal(FailureKind.REJECTION)
    assert classify(outcome.failure) is FailureClass.NON_RETRYABLE


@respx.mock
def test_a_client_error_status_is_a_refusal() -> None:
    respx.post(PAGE_URL).mock(return_value=httpx.Response(400, text="bad request"))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.NOT_DELIVERED
    assert outcome.failure == _signal(FailureKind.REJECTION)


# --- the vendor answered and we could not read it --------------------------------


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"unexpected": "shape"}),
    httpx.Response(200, json=["a", "list"]),
])
def test_an_unreadable_answer_leaves_the_outcome_unknown(response: httpx.Response) -> None:
    """A 200 we cannot parse may already have moved the label, so it is not a clean failure."""

    respx.post(PAGE_URL).mock(return_value=response)

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.UNKNOWN
    assert outcome.failure == _signal(FailureKind.UNEXPECTED_RESPONSE)


# --- the request never arrived: safe to retry ------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx.ConnectError("refused"), FailureKind.UNAVAILABLE),
        (httpx.ConnectTimeout("timed out"), FailureKind.TIMEOUT),
    ],
)
def test_a_request_that_never_left_is_not_delivered_and_retryable(
    error: Exception, kind: FailureKind
) -> None:
    respx.post(PAGE_URL).mock(side_effect=error)

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.NOT_DELIVERED
    assert outcome.failure == _signal(kind)
    assert classify(outcome.failure) is FailureClass.RETRYABLE


@respx.mock
@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_that_did_not_process_the_batch_is_retryable(status: int) -> None:
    respx.post(PAGE_URL).mock(return_value=httpx.Response(status, text="gateway"))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.NOT_DELIVERED
    assert outcome.failure == _signal(FailureKind.UNAVAILABLE)
    assert classify(outcome.failure) is FailureClass.RETRYABLE


# --- the request may have been applied: never resent automatically ---------------


@respx.mock
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("no answer"),
        httpx.ReadError("connection cut"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_a_sent_request_with_no_answer_is_operator_action_required(error: Exception) -> None:
    """FR-013: an unknown external outcome is reconciled, never resent by the runner."""

    respx.post(PAGE_URL).mock(side_effect=error)

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.UNKNOWN
    assert outcome.failure == _signal(FailureKind.OUTCOME_UNKNOWN)
    assert classify(outcome.failure) is FailureClass.OPERATOR_ACTION_REQUIRED


@respx.mock
def test_a_server_error_leaves_the_outcome_unknown() -> None:
    """A 500 arrived at the application, so the batch may already have been applied."""

    respx.post(PAGE_URL).mock(return_value=httpx.Response(500, text="boom"))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    assert outcome.certainty is DeliveryCertainty.UNKNOWN
    assert outcome.failure == _signal(FailureKind.OUTCOME_UNKNOWN)


# --- nothing leaks ---------------------------------------------------------------


@respx.mock
def test_no_outcome_carries_the_endpoint_or_the_driver_text() -> None:
    """The receipt and signal are audit rows; a host or driver message is neither."""

    respx.post(PAGE_URL).mock(side_effect=httpx.ConnectError("connect to aims.internal:9001 failed"))

    outcome = client().change_pages(STORE, CHANGES, KEY)

    rendered = repr(outcome)
    assert "aims.internal" not in rendered
    assert "9001" not in rendered
    assert "connect to" not in rendered


def _signal(kind: FailureKind):
    from esl_service.domain.failures import FailureSignal

    return FailureSignal(DependencyKind.AIMS_API, kind)
