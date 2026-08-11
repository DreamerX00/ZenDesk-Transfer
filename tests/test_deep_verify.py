"""
Tests for phase4_verify._deep_verify — the per-object content/order/status
comparison that replaces the old count-only check.
"""

from __future__ import annotations

import os
import sys
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


class FakeClient:
    def __init__(self, data: dict):
        self._data = data  # api_path -> list[dict]

    def list_resource(self, path: str, rkey: str):
        return self._data.get(path, [])


def _row(rows, label):
    return next(r for r in rows if r["Resource"] == label)


def test_exact_copy_passes(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase4_verify import _deep_verify

    src = {"triggers": [
        {"title": "A", "position": 1, "active": True},
        {"title": "B", "position": 2, "active": False},
    ]}
    tgt = {"triggers": [
        {"title": "A", "position": 1, "active": True},
        {"title": "B", "position": 2, "active": False},
    ]}
    rows, ok = _deep_verify(FakeClient(src), FakeClient(tgt))
    r = _row(rows, "Triggers")
    assert r["Matched"] == "2" and r["Missing"] == "0"
    assert r["Status≠"] == "0" and r["Order"] == "✅"
    assert ok is True


def test_wrong_order_fails(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase4_verify import _deep_verify

    src = {"macros": [
        {"title": "A", "position": 1, "active": True},
        {"title": "B", "position": 2, "active": True},
    ]}
    tgt = {"macros": [  # order swapped on target
        {"title": "A", "position": 2, "active": True},
        {"title": "B", "position": 1, "active": True},
    ]}
    rows, ok = _deep_verify(FakeClient(src), FakeClient(tgt))
    assert _row(rows, "Macros")["Order"] == "❌"
    assert ok is False


def test_status_mismatch_fails(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase4_verify import _deep_verify

    src = {"views": [{"title": "V", "position": 1, "active": True}]}
    tgt = {"views": [{"title": "V", "position": 1, "active": False}]}
    rows, ok = _deep_verify(FakeClient(src), FakeClient(tgt))
    assert _row(rows, "Views")["Status≠"] == "1"
    assert ok is False


def test_missing_object_fails(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase4_verify import _deep_verify

    src = {"automations": [
        {"title": "A", "position": 1, "active": True},
        {"title": "B", "position": 2, "active": True},
    ]}
    tgt = {"automations": [{"title": "A", "position": 1, "active": True}]}
    rows, ok = _deep_verify(FakeClient(src), FakeClient(tgt))
    r = _row(rows, "Automations")
    assert r["Missing"] == "1"
    assert ok is False


def test_relative_order_robust_to_system_slots(tmp_path: Path) -> None:
    """Custom fields at source positions 7,8 vs target 1,2 are still the SAME
    relative order — must PASS (we compare sequence, not absolute position)."""
    _fresh(tmp_path)
    from src.phases.phase4_verify import _deep_verify

    src = {"ticket_fields": [
        {"title": "Region", "position": 7, "active": True},
        {"title": "Tier", "position": 8, "active": True},
    ]}
    tgt = {"ticket_fields": [
        {"title": "Region", "position": 1, "active": True},
        {"title": "Tier", "position": 2, "active": True},
    ]}
    rows, ok = _deep_verify(FakeClient(src), FakeClient(tgt))
    assert _row(rows, "Ticket Fields")["Order"] == "✅"
    assert ok is True
