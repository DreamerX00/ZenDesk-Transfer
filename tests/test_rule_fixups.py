"""
Tests for the Phase 5 post-user fixup pass:
  - _restore_rule_restrictions  (personal/group visibility re-applied)
  - _restore_rule_user_conditions (stale source user IDs re-remapped)
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


def test_user_restriction_reapplied(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _restore_rule_restrictions

    client = FakeClient()
    exports = {"views": [
        {"id": 5, "title": "My personal view",
         "restriction": {"type": "User", "id": 100}},
    ]}
    id_map = {"views": {"5": "55"}, "users": {"100": "900"}, "groups": {}}

    _restore_rule_restrictions(client, id_map, exports)

    assert client.puts == [
        ("views/55", {"view": {"restriction": {"type": "User", "id": 900}}}),
    ]


def test_restriction_to_unmigrated_user_is_manual_not_put(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _restore_rule_restrictions

    client = FakeClient()
    exports = {"macros": [
        {"id": 1, "title": "Personal macro",
         "restriction": {"type": "User", "id": 777}},
    ]}
    id_map = {"macros": {"1": "11"}, "users": {}, "groups": {}}

    _restore_rule_restrictions(client, id_map, exports)
    assert client.puts == []  # no dead-id PUT; surfaced as MANUAL instead


def test_stale_user_condition_remapped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _restore_rule_user_conditions

    client = FakeClient()
    exports = {"triggers": [
        {"id": 3, "title": "Assign to Bob",
         "conditions": {"all": [{"field": "assignee_id", "operator": "is", "value": "100"}],
                        "any": []},
         "actions": [{"field": "assignee_id", "value": "100"}]},
    ]}
    id_map = {"triggers": {"3": "33"}, "users": {"100": "900"}, "groups": {}}

    _restore_rule_user_conditions(client, id_map, exports)

    assert len(client.puts) == 1
    path, payload = client.puts[0]
    assert path == "triggers/33"
    body = payload["trigger"]
    assert body["conditions"]["all"][0]["value"] == "900"  # remapped to target user
    assert body["actions"][0]["value"] == "900"


def test_rule_without_user_refs_untouched(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _restore_rule_user_conditions

    client = FakeClient()
    exports = {"triggers": [
        {"id": 3, "title": "Priority only",
         "conditions": {"all": [{"field": "priority", "operator": "is", "value": "high"}]}},
    ]}
    id_map = {"triggers": {"3": "33"}, "users": {"1": "2"}}

    _restore_rule_user_conditions(client, id_map, exports)
    assert client.puts == []  # no user refs → no PUT
