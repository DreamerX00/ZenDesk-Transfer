"""
Unit tests for src.importer.restore_positions — the shared position-restore
pass that reproduces source ordering on the target (Zendesk ignores `position`
on create, so order must be PUT afterward).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.importer import restore_positions


class FakeClient:
    """Records PUT calls; returns a normal (non-dry-run) response."""

    def __init__(self, dry_run: bool = False):
        self.puts: list[tuple[str, dict]] = []
        self._dry_run = dry_run

    def put(self, path: str, payload: dict) -> dict:
        self.puts.append((path, payload))
        return {"dry_run": True} if self._dry_run else {"ok": True}


def _map(**kw):
    return dict(kw)


def test_puts_position_for_mapped_items() -> None:
    client = FakeClient()
    id_map = {"ticket_fields": {"1": "101", "2": "102"}}
    items = [
        {"id": 1, "title": "A", "position": 7},
        {"id": 2, "title": "B", "position": 8},
    ]
    restore_positions(
        client, id_map, items,
        id_map_key="ticket_fields",
        update_path_fn=lambda tid: f"ticket_fields/{tid}",
        wrap_key="ticket_field",
    )
    assert client.puts == [
        ("ticket_fields/101", {"ticket_field": {"position": 7}}),
        ("ticket_fields/102", {"ticket_field": {"position": 8}}),
    ]


def test_skips_items_missing_from_id_map() -> None:
    """System fields (not in id_map) must not be repositioned — this is what
    keeps custom fields in the correct relative order around fixed system slots."""
    client = FakeClient()
    id_map = {"ticket_fields": {"2": "102"}}  # id 1 absent
    items = [
        {"id": 1, "title": "System", "position": 3},
        {"id": 2, "title": "Custom", "position": 9},
    ]
    restore_positions(
        client, id_map, items,
        id_map_key="ticket_fields",
        update_path_fn=lambda tid: f"ticket_fields/{tid}",
        wrap_key="ticket_field",
    )
    assert client.puts == [("ticket_fields/102", {"ticket_field": {"position": 9}})]


def test_skips_items_without_position() -> None:
    client = FakeClient()
    id_map = {"macros": {"5": "505"}}
    items = [{"id": 5, "title": "No pos"}]  # no 'position' key
    restore_positions(
        client, id_map, items,
        id_map_key="macros",
        update_path_fn=lambda tid: f"macros/{tid}",
        wrap_key="macro",
    )
    assert client.puts == []


def test_dry_run_still_issues_put_but_is_safe() -> None:
    """Dry-run client returns {'dry_run': True}; helper must not crash and must
    count it as skipped (no assertion error path)."""
    client = FakeClient(dry_run=True)
    id_map = {"sla_policies": {"1": "11"}}
    items = [{"id": 1, "title": "First reply", "position": 1}]
    restore_positions(
        client, id_map, items,
        id_map_key="sla_policies",
        update_path_fn=lambda tid: f"slas/policies/{tid}",
        wrap_key="sla_policy",
    )
    # The PUT is attempted (dry-run is decided server-side), payload well-formed.
    assert client.puts == [("slas/policies/11", {"sla_policy": {"position": 1}})]


def test_empty_or_missing_map_is_noop() -> None:
    client = FakeClient()
    restore_positions(
        client, {}, [{"id": 1, "position": 1}],
        id_map_key="views",
        update_path_fn=lambda tid: f"views/{tid}",
        wrap_key="view",
    )
    assert client.puts == []
