"""Deterministic recursive differences for canonical record payloads."""

from dataclasses import dataclass

from esl_service.domain.serialization import JSONValue, canonical_payload


@dataclass(frozen=True)
class FieldDifference:
    """A path-level difference between two canonical JSON-compatible values."""

    path: str
    old_value: JSONValue
    new_value: JSONValue


def diff_records(left: object, right: object) -> tuple[FieldDifference, ...]:
    """Return path-sorted, recursive canonical payload differences."""
    return diff_payloads(canonical_payload(left), canonical_payload(right))


def diff_payloads(left: JSONValue, right: JSONValue) -> tuple[FieldDifference, ...]:
    """Diff two already-canonical payloads, such as rows reloaded from JSONB.

    This makes a comparison reproducible from durable state after a restart or
    retry without re-reading a physical file (FR-027). Inputs must already be
    canonical; untyped values are never converted here.
    """
    differences: list[FieldDifference] = []
    _diff_values(left, right, "", differences)
    return tuple(sorted(differences, key=lambda difference: difference.path))


def _diff_values(
    left: JSONValue,
    right: JSONValue,
    path: str,
    differences: list[FieldDifference],
) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            child_path = f"{path}.{key}" if path else key
            if key not in left:
                differences.append(FieldDifference(child_path, None, right[key]))
            elif key not in right:
                differences.append(FieldDifference(child_path, left[key], None))
            else:
                _diff_values(left[key], right[key], child_path, differences)
        return
    if isinstance(left, list) and isinstance(right, list):
        for index in range(max(len(left), len(right))):
            child_path = f"{path}[{index}]"
            if index == len(left):
                differences.append(FieldDifference(child_path, None, right[index]))
            elif index == len(right):
                differences.append(FieldDifference(child_path, left[index], None))
            else:
                _diff_values(left[index], right[index], child_path, differences)
        return
    if left != right:
        differences.append(FieldDifference(path, left, right))
