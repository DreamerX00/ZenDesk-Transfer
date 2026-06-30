"""
Unit tests for conditional-field (dynamic form) migration.

Covers src.remapper.remap_form_conditions, which translates a ticket form's
agent_conditions / end_user_conditions from source ticket field IDs to target
IDs and drops rules anchored to non-migrated fields.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_remaps_parent_and_child_ids() -> None:
    from src.remapper import remap_form_conditions

    conditions = [
        {
            "parent_field_id": 100,
            "value": "urgent",
            "child_fields": [
                {"id": 200, "is_required": True, "required_on_statuses": {"type": "ALL"}},
                {"id": 201, "is_required": False},
            ],
        }
    ]
    id_map = {"ticket_fields": {"100": "900", "200": "910", "201": "911"}}

    result = remap_form_conditions(conditions, id_map)

    assert len(result) == 1
    cond = result[0]
    assert cond["parent_field_id"] == 900
    assert cond["value"] == "urgent"  # option value preserved verbatim
    assert [c["id"] for c in cond["child_fields"]] == [910, 911]
    # Non-ID attributes are preserved.
    assert cond["child_fields"][0]["is_required"] is True
    assert cond["child_fields"][0]["required_on_statuses"] == {"type": "ALL"}


def test_drops_condition_when_parent_unmapped() -> None:
    from src.remapper import remap_form_conditions

    conditions = [
        {"parent_field_id": 999, "value": "x", "child_fields": [{"id": 200}]},
        {"parent_field_id": 100, "value": "y", "child_fields": [{"id": 200}]},
    ]
    id_map = {"ticket_fields": {"100": "900", "200": "910"}}

    result = remap_form_conditions(conditions, id_map)

    # First condition dropped (parent 999 not mapped); second kept.
    assert len(result) == 1
    assert result[0]["parent_field_id"] == 900


def test_drops_only_unmapped_child_fields() -> None:
    from src.remapper import remap_form_conditions

    conditions = [
        {
            "parent_field_id": 100,
            "value": "z",
            "child_fields": [
                {"id": 200},
                {"id": 999},  # not migrated — should be dropped
                {"id": 201},
            ],
        }
    ]
    id_map = {"ticket_fields": {"100": "900", "200": "910", "201": "911"}}

    result = remap_form_conditions(conditions, id_map)

    assert len(result) == 1
    assert [c["id"] for c in result[0]["child_fields"]] == [910, 911]


def test_returns_empty_when_field_map_missing() -> None:
    from src.remapper import remap_form_conditions

    conditions = [{"parent_field_id": 100, "value": "a", "child_fields": []}]

    assert remap_form_conditions(conditions, {}) == []
    assert remap_form_conditions(conditions, {"ticket_fields": None}) == []


def test_handles_non_list_input() -> None:
    from src.remapper import remap_form_conditions

    id_map = {"ticket_fields": {"100": "900"}}

    assert remap_form_conditions(None, id_map) == []
    assert remap_form_conditions({}, id_map) == []


def test_skips_malformed_entries() -> None:
    from src.remapper import remap_form_conditions

    conditions = [
        "not-a-dict",
        {"parent_field_id": 100, "value": "ok", "child_fields": ["bad", {"id": 200}]},
    ]
    id_map = {"ticket_fields": {"100": "900", "200": "910"}}

    result = remap_form_conditions(conditions, id_map)

    assert len(result) == 1
    assert result[0]["parent_field_id"] == 900
    assert [c["id"] for c in result[0]["child_fields"]] == [910]


def test_digit_string_and_int_map_values() -> None:
    from src.remapper import remap_form_conditions

    conditions = [{"parent_field_id": 100, "value": "v", "child_fields": [{"id": 200}]}]
    # Map values may be stored as ints in some id_map states.
    id_map = {"ticket_fields": {"100": 900, "200": "910"}}

    result = remap_form_conditions(conditions, id_map)

    assert result[0]["parent_field_id"] == 900
    assert result[0]["child_fields"][0]["id"] == 910


def _run_all() -> int:
    tests = [
        test_remaps_parent_and_child_ids,
        test_drops_condition_when_parent_unmapped,
        test_drops_only_unmapped_child_fields,
        test_returns_empty_when_field_map_missing,
        test_handles_non_list_input,
        test_skips_malformed_entries,
        test_digit_string_and_int_map_values,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    return failed


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="zdx-cond-"):
        sys.exit(_run_all())
