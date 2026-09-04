"""The AIMS page-change adapter: the one path that mutates AIMS (#23, AD-021).

The contract of record is the deployed Dashboard operation
``POST /dashboardservice/common/labels/page?store=<code>`` carrying a
``pageChangeList`` of ``labelCode``/``page`` entries. The document itself is
now captured at ``docs/hop-jenkins-pipeline/AIMS-Dashboard-OpenAPI-common.json``
and the contract test reads it, so this adapter cannot drift from it silently.

The document decides the outcome, and it is deliberately thin: it names
``200 OK``, ``402 License is expired``, ``405 Parameter is invalid``, and
``500``, and its ``AimsApiResponse`` marks *no* field required. There is
therefore no documented vocabulary of success or failure values for
``responseCode``. This adapter does not invent one. HTTP status decides, and
``responseCode``, ``responseMessage``, and ``customBatchId`` are recorded as
receipt evidence for the operator. Were the vendor ever to answer 200 while
declining in the body, the #24 read model and #25 reconciliation are what
catch it, not a guess made here.

AD-021 accepts that the vendor declares no authentication scheme and that
the current pipeline already posts to this endpoint unauthenticated. What
this adapter adds is the discipline the current pipeline lacks, and the
sharpest part of it is the line between two kinds of failure:

* the request demonstrably never arrived (connection refused, connect
  timeout, a gateway that did not process it), which is ``NOT_DELIVERED``
  and retryable under the section 8 matrix; and
* the request was written and no usable answer came back (read timeout, a
  cut connection, a 500, a body we cannot parse), which is ``UNKNOWN`` and
  is reconciled by an operator, never resent by the runner (FR-013).

Collapsing the second into the first is how a page change silently happens
twice, so the adapter never guesses in that direction.

Writes to any AIMS database remain forbidden (AD-002): this module speaks
HTTP only and holds no database engine.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from esl_service.application.contracts import (
    PageChange,
    PageChangeOutcome,
    PageChangeReceipt,
)
from esl_service.domain.actions import DeliveryCertainty
from esl_service.domain.failures import DependencyKind, FailureKind, FailureSignal

#: The operation of record, relative to the configured service base URL.
PAGE_CHANGE_PATH = "/common/labels/page"

#: Sent on every submission. The vendor documents no idempotency support, so
#: this is our own key (#19), carried so a retried attempt is recognisable in
#: the vendor's logs and in ours. Safety does not rest on it: setting a label
#: to a page is idempotent in effect, and the action ledger is authoritative.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Documented refusals of the operation of record. Both need a human, so
#: neither is ever retried.
_LICENSE_EXPIRED = 402
_INVALID_PARAMETER = 405


class PageChangeRequestError(ValueError):
    """The caller asked for something that must never reach the vendor."""


class AimsPageClientHttp:
    """Submits label page changes over HTTP and reports a typed outcome.

    Implements ``AimsPageClient`` (#22). It reports rather than raises, so an
    interrupted call stays representable as ``DeliveryCertainty.UNKNOWN``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise PageChangeRequestError("base_url must name the AIMS Dashboard service")
        if timeout_seconds < 1:
            raise PageChangeRequestError("timeout_seconds must be at least one second")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Any) -> "AimsPageClientHttp":
        """Build from configuration. The base URL is a host, never a secret."""

        return cls(
            base_url=settings.aims_dashboard_base_url,
            timeout_seconds=settings.aims_dashboard_timeout_seconds,
        )

    def change_pages(
        self,
        store_code: str,
        changes: Sequence[PageChange],
        idempotency_key: str,
    ) -> PageChangeOutcome:
        """Submit one batch for one store and report what is known about it."""

        if not store_code.strip():
            raise PageChangeRequestError("store_code must name a store")
        if not changes:
            raise PageChangeRequestError("a batch must carry at least one page change")
        if not idempotency_key.strip():
            raise PageChangeRequestError("idempotency_key must be supplied by the caller")

        payload = {
            "pageChangeList": [
                {"labelCode": change.label_code, "page": change.page} for change in changes
            ]
        }
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                response = http.post(
                    f"{self._base_url}{PAGE_CHANGE_PATH}",
                    params={"store": store_code},
                    json=payload,
                    headers={IDEMPOTENCY_HEADER: idempotency_key},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # Nothing was written to the wire, so the batch cannot have applied.
            kind = (
                FailureKind.TIMEOUT
                if isinstance(error, httpx.ConnectTimeout)
                else FailureKind.UNAVAILABLE
            )
            return _not_delivered(kind)
        except httpx.HTTPError:
            # The request was written and no usable answer came back. It may
            # already have moved a label, so it is never resent automatically.
            return _unknown(FailureKind.OUTCOME_UNKNOWN)

        return _read(response)


def _read(response: httpx.Response) -> PageChangeOutcome:
    """Turn one answered request into a classified outcome."""

    status = response.status_code
    if status in (502, 503, 504):
        # A gateway refused or could not reach the service behind it.
        return _not_delivered(FailureKind.UNAVAILABLE)
    if status >= 500:
        # The application failed after receiving the batch, so the change may
        # already have been applied.
        return _unknown(FailureKind.OUTCOME_UNKNOWN)
    if status in (_LICENSE_EXPIRED, _INVALID_PARAMETER) or status >= 400:
        # A documented refusal, or any other client error: a human corrects
        # the licence or the request, and resending cannot help.
        return _not_delivered(FailureKind.REJECTION)

    body = _parse(response)
    if body is None:
        return _unknown(FailureKind.UNEXPECTED_RESPONSE)
    return PageChangeOutcome(
        certainty=DeliveryCertainty.CONFIRMED,
        receipt=PageChangeReceipt(
            response_code=_text(body.get("responseCode")),
            response_message=_text(body.get("responseMessage")),
            custom_batch_id=_optional_text(body.get("customBatchId")),
        ),
    )


def _text(value: object) -> str:
    """Every AimsApiResponse field is optional in the schema, so absence is empty."""

    return str(value) if isinstance(value, str | int) else ""


def _optional_text(value: object) -> str | None:
    return str(value) if isinstance(value, str | int) else None


def _parse(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        body = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _not_delivered(kind: FailureKind) -> PageChangeOutcome:
    return PageChangeOutcome(
        certainty=DeliveryCertainty.NOT_DELIVERED,
        failure=FailureSignal(DependencyKind.AIMS_API, kind),
    )


def _unknown(kind: FailureKind) -> PageChangeOutcome:
    return PageChangeOutcome(
        certainty=DeliveryCertainty.UNKNOWN,
        failure=FailureSignal(DependencyKind.AIMS_API, kind),
    )


__all__ = [
    "IDEMPOTENCY_HEADER",
    "PAGE_CHANGE_PATH",
    "AimsPageClientHttp",
    "PageChangeRequestError",
]
