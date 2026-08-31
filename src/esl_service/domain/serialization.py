"""Deterministic canonical payloads, hashes, and secret-safe evidence."""

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum

type JSONValue = (
    None | bool | int | str | list["JSONValue"] | dict[str, "JSONValue"]
)


def canonical_payload(value: object) -> JSONValue:
    """Convert supported immutable domain values to deterministic JSON-compatible data."""
    return _json_value(value)


def canonical_hash(value: object) -> str:
    """Return the deterministic UTF-8, sorted-key SHA-256 canonical hash."""
    payload = canonical_payload(value)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_value(value: object) -> JSONValue:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not a canonical timestamp")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


#: Key fragments that may never appear in persisted evidence (NFR-009).
FORBIDDEN_EVIDENCE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "connection_string",
    "database_url",
    "dpapi",
)


def sanitize_evidence(value: JSONValue) -> JSONValue:
    """Return evidence unchanged, refusing any secret-like key.

    Diagnostic evidence is persisted and shown to operators, so a credential
    must never reach it. The check is recursive and case-insensitive, and it
    raises rather than redacting so the caller fixes the source of the leak
    instead of shipping a silently truncated record.
    """

    if isinstance(value, dict):
        for key in value:
            lowered = key.casefold()
            for fragment in FORBIDDEN_EVIDENCE_KEY_FRAGMENTS:
                if fragment in lowered:
                    raise ValueError(f"forbidden evidence key: {key}")
        return {key: sanitize_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value]
    return value
