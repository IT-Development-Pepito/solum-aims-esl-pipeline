"""Deterministic JSON-compatible canonical payload and hash functions."""

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
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
    if isinstance(value, (date, datetime, time)):
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
