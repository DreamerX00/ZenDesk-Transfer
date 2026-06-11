"""
Unit tests for the trigger / trigger-category migration logic in
src/phases/phase2_business_logic.

Tests the two hook functions:
  - _prepare_trigger     (pre_process_fn)
  - _assign_trigger_category  (post_process_fn)

without touching the Zendesk API.
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


# ------------------------------------------------------------------ #
#  _prepare_trigger                                                    #
# ------------------------------------------------------------------ #

def test_prepare_trigger_remaps_category_id(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    item = {"id": 42, "title": "Notify support", "category_id": 7, "active": True}
    id_map = {
        "trigger_categories": {"7": "99"},
        "groups": {},
    }

    result = _prepare_trigger(item, id_map)

    assert result is not None
    assert "category_id" not in result
    assert result["_trigger_category_id"] == "99"
    assert result["id"] == 42
    assert result["title"] == "Notify support"


def test_prepare_trigger_drops_unmapped_category(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    item = {"id": 7, "title": "Orphaned cat", "category_id": 999, "active": True}
    id_map = {
        "trigger_categories": {"1": "10"},
    }

    result = _prepare_trigger(item, id_map)

    assert result is not None
    assert "category_id" not in result
    assert "_trigger_category_id" not in result


def test_prepare_trigger_no_category(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    item = {"id": 1, "title": "Simple trigger", "active": True}
    id_map = {"trigger_categories": {}}

    result = _prepare_trigger(item, id_map)

    assert result is not None
    assert "category_id" not in result
    assert "_trigger_category_id" not in result


def test_prepare_trigger_handles_empty_map(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    item = {"id": 1, "title": "No cats", "category_id": 5}
    id_map = {}

    result = _prepare_trigger(item, id_map)

    assert result is not None
    assert "category_id" not in result
    assert "_trigger_category_id" not in result


def test_prepare_trigger_strips_restriction(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    item = {
        "id": 5,
        "title": "Restricted trigger",
        "category_id": 7,
        "restriction": {"type": "Group", "id": 999},
    }
    id_map = {
        "trigger_categories": {"7": "99"},
        "groups": {"100": "200"},  # group 999 NOT present
    }

    result = _prepare_trigger(item, id_map)

    assert result is not None
    assert "restriction" not in result
    assert result["_trigger_category_id"] == "99"


def test_prepare_trigger_passes_through_none(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _prepare_trigger

    result = _prepare_trigger(None, {})

    assert result is None


# ------------------------------------------------------------------ #
#  _assign_trigger_category                                            #
# ------------------------------------------------------------------ #

def test_assign_restores_category_id(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_trigger_category

    payload = {"title": "My trigger", "_trigger_category_id": "99", "active": True}
    id_map = {}

    result = _assign_trigger_category(payload, id_map)

    assert "_trigger_category_id" not in result
    assert result["category_id"] == 99
    assert result["title"] == "My trigger"


def test_assign_noop_without_stash(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_trigger_category

    payload = {"title": "My trigger", "active": True}
    id_map = {}

    result = _assign_trigger_category(payload, id_map)

    assert "category_id" not in result
    assert "_trigger_category_id" not in result


def test_assign_preserves_string_category(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_trigger_category

    payload = {"_trigger_category_id": "abc-def"}
    id_map = {}

    result = _assign_trigger_category(payload, id_map)

    assert result["category_id"] == "abc-def"


# ------------------------------------------------------------------ #
#  Manual harness                                                      #
# ------------------------------------------------------------------ #

def _run_all() -> int:
    tests = [
        test_prepare_trigger_remaps_category_id,
        test_prepare_trigger_drops_unmapped_category,
        test_prepare_trigger_no_category,
        test_prepare_trigger_handles_empty_map,
        test_prepare_trigger_strips_restriction,
        test_prepare_trigger_passes_through_none,
        test_assign_restores_category_id,
        test_assign_noop_without_stash,
        test_assign_preserves_string_category,
    ]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-trigger-") as td:
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
