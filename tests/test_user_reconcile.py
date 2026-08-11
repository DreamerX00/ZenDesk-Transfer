"""
Tests for Part 4: _reconcile_existing_user — email-collision path now pushes
safe profile attributes onto the pre-existing target user, but never changes
role/verified (which could lock the account owner).
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
    def __init__(self):
        self.puts = []

    def put(self, path, payload):
        self.puts.append((path, payload))
        return {"ok": True}


def test_reconcile_pushes_safe_attrs_not_role(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reconcile_existing_user

    client = FakeClient()
    source_user = {
        "id": 1, "name": "Alice Owner", "email": "a@x.com",
        "role": "admin", "notes": "VIP", "time_zone": "UTC",
        "tags": ["gold"], "organization_id": 7,
    }
    target_user = {"id": 900, "name": "Alice", "role": "end-user"}
    id_map = {"organizations": {"7": "70"}, "users": {}}

    _reconcile_existing_user(client, id_map, source_user, target_user, "Alice Owner")

    assert len(client.puts) == 1
    path, payload = client.puts[0]
    assert path == "users/900"
    user = payload["user"]
    # Safe attributes are pushed...
    assert user["name"] == "Alice Owner"
    assert user["notes"] == "VIP"
    assert user["tags"] == ["gold"]
    assert user["organization_id"] == 70  # remapped
    # ...but role/verified are NOT touched (owner-safety).
    assert "role" not in user
    assert "verified" not in user


def test_reconcile_noop_when_no_safe_attrs(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reconcile_existing_user

    client = FakeClient()
    # Only role differs, nothing safe to push → no PUT (role change is MANUAL).
    source_user = {"id": 1, "role": "admin"}
    target_user = {"id": 900, "role": "end-user"}

    _reconcile_existing_user(client, {"users": {}}, source_user, target_user, "X")
    assert client.puts == []
