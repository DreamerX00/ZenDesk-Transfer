"""
Unit tests for macro action remapping (gap #2) and custom-field-value
sanitization (gap #1) in src.remapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ------------------------------------------------------------------ #
#  remap_macro_actions                                                #
# ------------------------------------------------------------------ #

def test_remaps_scalar_group_id() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "group_id", "value": "10"}]
    id_map = {"groups": {"10": "20"}}

    out = remap_macro_actions(actions, id_map)

    assert out == [{"field": "group_id", "value": "20"}]


def test_drops_action_with_unmapped_group() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "group_id", "value": "999"}, {"field": "priority", "value": "high"}]
    id_map = {"groups": {"10": "20"}}

    out = remap_macro_actions(actions, id_map)

    # Unmapped group action dropped; non-ID action preserved.
    assert out == [{"field": "priority", "value": "high"}]


def test_passes_through_non_id_actions() -> None:
    from src.remapper import remap_macro_actions

    actions = [
        {"field": "set_tags", "value": ["a", "b"]},
        {"field": "comment_value", "value": "Hello"},
        {"field": "status", "value": "solved"},
    ]
    out = remap_macro_actions(actions, {"groups": {}})

    assert out == actions


def test_remaps_custom_field_action() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "custom_fields_100", "value": "opt_a"}]
    id_map = {"ticket_fields": {"100": "900"}}

    out = remap_macro_actions(actions, id_map)

    assert out == [{"field": "custom_fields_900", "value": "opt_a"}]


def test_drops_custom_field_action_when_unmapped() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "custom_fields_999", "value": "x"}]
    id_map = {"ticket_fields": {"100": "900"}}

    assert remap_macro_actions(actions, id_map) == []


def test_assignee_composite_both_halves() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "assignee_id", "value": "10/50"}]
    id_map = {"groups": {"10": "20"}, "users": {"50": "60"}}

    out = remap_macro_actions(actions, id_map)

    assert out == [{"field": "assignee_id", "value": "20/60"}]


def test_assignee_composite_group_only() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "assignee_id", "value": "10/"}]
    id_map = {"groups": {"10": "20"}, "users": {}}

    out = remap_macro_actions(actions, id_map)

    assert out == [{"field": "assignee_id", "value": "20/"}]


def test_assignee_composite_drops_when_neither_resolves() -> None:
    from src.remapper import remap_macro_actions

    actions = [{"field": "assignee_id", "value": "999/888"}]
    id_map = {"groups": {"10": "20"}, "users": {"50": "60"}}

    assert remap_macro_actions(actions, id_map) == []


def test_assignee_composite_keeps_resolved_half_only() -> None:
    from src.remapper import remap_macro_actions

    # group resolves, user does not — keep group, blank the user half.
    actions = [{"field": "assignee_id", "value": "10/888"}]
    id_map = {"groups": {"10": "20"}, "users": {"50": "60"}}

    out = remap_macro_actions(actions, id_map)

    assert out == [{"field": "assignee_id", "value": "20/"}]


def test_non_list_input_returns_empty() -> None:
    from src.remapper import remap_macro_actions

    assert remap_macro_actions(None, {}) == []
    assert remap_macro_actions({}, {}) == []


def test_skips_malformed_action_entries() -> None:
    from src.remapper import remap_macro_actions

    actions = ["bad", {"no_field": 1}, {"field": "group_id", "value": "10"}]
    id_map = {"groups": {"10": "20"}}

    out = remap_macro_actions(actions, id_map)

    # The {"no_field":1} dict has no string `field` → passed through;
    # "bad" string skipped; group remapped.
    assert {"field": "group_id", "value": "20"} in out
    assert {"no_field": 1} in out
    assert "bad" not in out


# ------------------------------------------------------------------ #
#  sanitize_custom_field_values                                       #
# ------------------------------------------------------------------ #

def test_sanitize_drops_none_values() -> None:
    from src.remapper import sanitize_custom_field_values

    out = sanitize_custom_field_values({"a": "1", "b": None, "c": 0})

    assert out == {"a": "1", "c": 0}


def test_sanitize_empty_returns_none() -> None:
    from src.remapper import sanitize_custom_field_values

    assert sanitize_custom_field_values({}) is None
    assert sanitize_custom_field_values({"x": None}) is None


def test_sanitize_non_dict_returns_none() -> None:
    from src.remapper import sanitize_custom_field_values

    assert sanitize_custom_field_values(None) is None
    assert sanitize_custom_field_values([1, 2]) is None


def test_sanitize_drops_non_string_keys() -> None:
    from src.remapper import sanitize_custom_field_values

    out = sanitize_custom_field_values({"good": "v", 5: "bad"})

    assert out == {"good": "v"}


def test_sanitize_preserves_falsey_non_null() -> None:
    from src.remapper import sanitize_custom_field_values

    out = sanitize_custom_field_values({"flag": False, "num": 0, "empty": ""})

    assert out == {"flag": False, "num": 0, "empty": ""}


def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
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
    sys.exit(_run_all())
