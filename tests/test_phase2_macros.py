"""
Unit tests for the macro pre/post-process hooks in
src.phases.phase2_business_logic:

  - _prepare_macro       (stashes actions so remap_payload can't touch them)
  - _assign_macro_actions (remaps + restores actions after remap_payload)

The key invariant: macro actions are remapped exactly once, with target IDs,
and the composite assignee format survives a full prepare → remap → assign
round-trip without being double-processed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh(tmp: Path) -> None:
    from server import settings as s_mod
    from server import state as st_mod
    os.environ["ZDX_DEV_MODE"] = "1"
    os.environ["ZDX_STATE_ROOT"] = str(tmp)
    os.environ["ZDX_CONNECTIONS_PATH"] = str(tmp / "connections.enc")
    s_mod._settings = None
    st_mod._store = None


def test_prepare_stashes_actions(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_macro

    item = {"id": 1, "title": "M", "actions": [{"field": "group_id", "value": "10"}]}
    out = _prepare_macro(item, {})

    assert "actions" not in out
    assert out["_macro_actions"] == [{"field": "group_id", "value": "10"}]


def test_prepare_passes_through_none(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_macro

    assert _prepare_macro(None, {}) is None


def test_prepare_macro_without_actions(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_macro

    out = _prepare_macro({"id": 1, "title": "M"}, {})
    assert "_macro_actions" not in out
    assert "actions" not in out


def test_assign_restores_remapped_actions(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_macro_actions

    payload = {
        "title": "M",
        "_macro_actions": [
            {"field": "group_id", "value": "10"},
            {"field": "assignee_id", "value": "10/50"},
            {"field": "set_tags", "value": ["x"]},
        ],
    }
    id_map = {"groups": {"10": "20"}, "users": {"50": "60"}}

    out = _assign_macro_actions(payload, id_map)

    assert "_macro_actions" not in out
    assert out["actions"] == [
        {"field": "group_id", "value": "20"},
        {"field": "assignee_id", "value": "20/60"},
        {"field": "set_tags", "value": ["x"]},
    ]


def test_full_round_trip_no_double_remap(tmp_path: Path) -> None:
    _fresh(tmp_path)
    # Simulate the importer pipeline: pre_process → remap_payload → post_process.
    from src.phases.phase2_business_logic import _prepare_macro, _assign_macro_actions
    from src.remapper import strip_source_fields, remap_payload

    item = {
        "id": 1,
        "title": "Escalate",
        "actions": [{"field": "group_id", "value": "10"}],
    }
    id_map = {"groups": {"10": "20"}}

    prepped = _prepare_macro(item, id_map)
    stripped = strip_source_fields(prepped)
    remapped = remap_payload(stripped, id_map, context="macros:Escalate")
    final = _assign_macro_actions(remapped, id_map)

    # group_id remapped exactly once to the target id — not dropped by a
    # second lookup of an already-target value.
    assert final["actions"] == [{"field": "group_id", "value": "20"}]


def test_assign_noop_without_stash(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_macro_actions

    payload = {"title": "M"}
    out = _assign_macro_actions(payload, {})
    assert "actions" not in out


def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-macro-") as td:
            try:
                t(Path(td))
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
