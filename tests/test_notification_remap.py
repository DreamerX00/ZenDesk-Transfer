"""
Tests for Part 2 remap-correctness fixes:
  - notification actions (list-valued) get their leading ID remapped
  - unmapped notification actions are dropped (not left with a dead source ID)
  - schedule_id is remapped as a scalar field
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


def test_notification_webhook_and_group_ids_remapped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import remap_payload

    trigger = {
        "actions": [
            {"field": "notification_webhook", "value": ["555", "subj", "body"]},
            {"field": "notification_group", "value": ["10", "subj", "body"]},
            {"field": "notification_sms_group", "value": ["10", "+100", "body"]},
        ]
    }
    id_map = {"webhooks": {"555": "999"}, "groups": {"10": "20"}}

    out = remap_payload(trigger, id_map, context="triggers:Notify")
    vals = [a["value"][0] for a in out["actions"]]
    assert vals == ["999", "20", "20"]
    # non-ID elements of the list are preserved
    assert out["actions"][0]["value"][1:] == ["subj", "body"]


def test_unmapped_notification_action_is_dropped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import remap_payload

    trigger = {
        "actions": [
            {"field": "notification_webhook", "value": ["777", "s", "b"]},  # unmapped
            {"field": "notification_group", "value": ["10", "s", "b"]},     # mapped
        ]
    }
    id_map = {"webhooks": {}, "groups": {"10": "20"}}

    out = remap_payload(trigger, id_map, context="triggers:Notify")
    # The dead-webhook action is removed entirely; the good one survives remapped.
    assert len(out["actions"]) == 1
    assert out["actions"][0]["field"] == "notification_group"
    assert out["actions"][0]["value"][0] == "20"


def test_schedule_id_scalar_remapped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import remap_payload

    payload = {"schedule_id": 5, "title": "Weekend SLA"}
    id_map = {"schedules": {"5": "50"}}

    out = remap_payload(payload, id_map, context="sla_policies:Weekend")
    assert out["schedule_id"] == 50
    assert out["title"] == "Weekend SLA"


def test_schedule_id_unmapped_left_but_surfaced(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import remap_payload

    payload = {"schedule_id": 9}
    id_map = {"schedules": {}}  # no mapping

    out = remap_payload(payload, id_map, context="sla_policies:X")
    # Stale value left in place (documented behaviour) — but a MANUAL note is
    # emitted so it isn't silent. We only assert the value survives here.
    assert out["schedule_id"] == 9
