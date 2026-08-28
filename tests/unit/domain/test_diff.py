"""FR-004/FR-005/BR-018 tests for deterministic canonical record differences."""

from dataclasses import replace
from decimal import Decimal

from esl_service.domain import diff_records
from tests.factories import canonical_record


def test_fr_004_diff_names_changed_paths() -> None:
    """FR-004 reports a stable path for each changed canonical value."""
    left = canonical_record()
    right = replace(
        left,
        pricing=replace(left.pricing, display_regular_price=Decimal(4500)),
    )

    assert [item.path for item in diff_records(left, right)] == [
        "pricing.display_regular_price"
    ]


def test_br_018_diff_reports_key_boundaries_without_cross_record_matching() -> None:
    """BR-018 represents a changed selling UOM as the canonical key field difference."""
    left = canonical_record()
    right = replace(left, key=replace(left.key, selling_uom="EA"))

    differences = diff_records(left, right)

    assert [(item.path, item.old_value, item.new_value) for item in differences] == [
        ("key.selling_uom", "KGS", "EA")
    ]


def test_fr_005_diff_is_pure_and_sorts_nested_paths() -> None:
    """FR-005 compares pure payload values in deterministic lexical path order."""
    left = canonical_record()
    right = replace(
        left,
        display_decision=replace(left.display_decision, desired_page=3),
        product=replace(left.product, brand="SOLUM SELECT"),
    )

    assert [item.path for item in diff_records(left, right)] == [
        "display_decision.desired_page",
        "product.brand",
    ]
