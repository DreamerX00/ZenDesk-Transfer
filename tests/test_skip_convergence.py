"""
Test for item 6.2 — conflict_mode='skip' now converges active/status on the
existing target item so re-runs become an exact copy.
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
    """Existing target trigger 'T' is ACTIVE; source wants it INACTIVE."""

    def __init__(self):
        self.puts = []

    def list_resource(self, path, rkey):
        return [{"id": 500, "title": "T", "active": True}]

    def put(self, path, payload):
        self.puts.append((path, payload))
        return {"ok": True}

    def post(self, *a, **k):
        raise AssertionError("skip mode must not POST a conflicting item")


def test_skip_converges_active_state(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.importer import import_resource

    client = FakeClient()
    id_map = {}
    source_items = [{"id": 1, "title": "T", "active": False}]  # source is inactive

    import_resource(
        client=client, id_map=id_map, source_items=source_items,
        resource_key="triggers",
        list_path="triggers", list_rkey="triggers",
        create_path="triggers", create_rkey="trigger",
        create_response_rkey="trigger",
        delete_path_fn=lambda t: f"triggers/{t}",
        update_path_fn=lambda t: f"triggers/{t}",
        name_field="title",
        conflict_mode="skip",
    )

    # Existing item mapped AND its active state converged to source (False).
    assert id_map["triggers"]["1"] == "500"
    assert ("triggers/500", {"trigger": {"active": False}}) in client.puts
