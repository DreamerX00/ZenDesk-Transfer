"""
Unit tests for ticket-form field dependency migration:

  - src.remapper.build_system_field_map      (map system fields by `type`)
  - src.phases.phase2_business_logic._assign_form_fields
        (full custom+system field map → ticket_field_ids + conditional rules)
  - src.importer.import_resource conflict_mode="skip" reconciliation
        (skipped resources are recorded in id_map so dependent phases —
         e.g. form conditions — resolve on re-runs / pre-populated targets)

These exercise the regression where dynamic form dependencies were dropped:
forms skipped on re-run were never mapped, and conditions anchored on system
fields (Type/Priority/...) were dropped because system fields aren't in id_map.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _reset_importer_globals() -> None:
    """Clear the importer's process-global id_map debounce bookkeeping so these
    tests neither inherit nor leak pending-write counters. Other suites
    (e.g. test_runctx_isolation) assert on an exact flush cadence that assumes
    a clean counter, so we must leave no residue behind."""
    from src import importer as imp
    with imp._write_lock:
        imp._pending_writes.clear()
        imp._last_flushed_ref.clear()


def _fresh(tmp: Path) -> None:
    from server import settings as s_mod
    from server import state as st_mod
    os.environ["ZDX_DEV_MODE"] = "1"
    os.environ["ZDX_STATE_ROOT"] = str(tmp)
    os.environ["ZDX_CONNECTIONS_PATH"] = str(tmp / "connections.enc")
    s_mod._settings = None
    st_mod._store = None
    _reset_importer_globals()


# ------------------------------------------------------------------ #
#  Fakes                                                              #
# ------------------------------------------------------------------ #

class FakeFormClient:
    """Stands in for ZendeskClient in _assign_form_fields."""

    def __init__(self, target_fields):
        self.dry_run = False
        self._target_fields = target_fields
        self.puts = []

    def list_resource(self, path, key):
        if path == "ticket_fields":
            return self._target_fields
        return []

    def put(self, path, payload):
        self.puts.append((path, payload))
        return {"ok": True}


class FakeImportClient:
    """Stands in for ZendeskClient in import_resource."""

    def __init__(self, existing):
        self.dry_run = False
        self._existing = existing
        self.posts = []
        self.deletes = []

    def list_resource(self, path, key):
        return self._existing

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {"group": {"id": 999}}

    def delete(self, path):
        self.deletes.append(path)


# ------------------------------------------------------------------ #
#  build_system_field_map                                            #
# ------------------------------------------------------------------ #

def test_build_system_field_map_matches_by_type(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import build_system_field_map

    source = [
        {"id": 10, "type": "tickettype"},
        {"id": 11, "type": "priority"},
        {"id": 12, "type": "tagger"},   # custom — ignored
    ]
    target = [
        {"id": 910, "type": "tickettype"},
        {"id": 911, "type": "priority"},
        {"id": 912, "type": "tagger"},   # custom — ignored
    ]
    m = build_system_field_map(source, target)

    assert m == {"10": "910", "11": "911"}


def test_build_system_field_map_ignores_custom_and_unmatched(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import build_system_field_map

    source = [
        {"id": 10, "type": "tickettype"},
        {"id": 11, "type": "group"},      # target has no group field
        {"id": 12, "type": "tagger"},     # custom
    ]
    target = [{"id": 910, "type": "tickettype"}]
    m = build_system_field_map(source, target)

    assert m == {"10": "910"}  # group unmatched, tagger ignored


def test_build_system_field_map_handles_malformed(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.remapper import build_system_field_map

    assert build_system_field_map(None, None) == {}
    assert build_system_field_map("x", []) == {}
    assert build_system_field_map(
        [{"type": "priority"}, "bad", {"id": 5}], [{"id": 9, "type": "priority"}]
    ) == {}  # source priority has no id → unmapped


# ------------------------------------------------------------------ #
#  _assign_form_fields — the dynamic-dependency regression           #
# ------------------------------------------------------------------ #

def test_assign_form_fields_system_parent_condition_survives(tmp_path: Path) -> None:
    """A condition anchored on the system Type field must survive: its
    parent_field_id is mapped by type, not via id_map['ticket_fields']."""
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_form_fields

    # Target's live fields: system Type at a NEW id, plus the custom fields.
    target_fields = [
        {"id": 910, "type": "tickettype", "title": "Type"},
        {"id": 920, "type": "tagger", "title": "Cloud"},
        {"id": 930, "type": "integer", "title": "Acct"},
    ]
    client = FakeFormClient(target_fields)
    id_map = {
        "ticket_forms": {"1": "100"},
        # Only custom fields are in id_map; the system Type field (10) is not.
        "ticket_fields": {"20": "920", "30": "930"},
    }
    source_forms = [{
        "id": 1, "name": "Tech",
        "ticket_field_ids": [10, 20, 30],
        "agent_conditions": [{
            "parent_field_id": 10, "value": "incident",
            "child_fields": [{
                "id": 30, "is_required": True,
                "required_on_statuses": {"type": "ALL_STATUSES"},
            }],
        }],
    }]
    source_fields = [
        {"id": 10, "type": "tickettype", "title": "Type"},
        {"id": 20, "type": "tagger", "title": "Cloud"},
        {"id": 30, "type": "integer", "title": "Acct"},
    ]

    _assign_form_fields(client, id_map, source_forms, source_fields)

    assert len(client.puts) == 1
    path, payload = client.puts[0]
    assert path == "ticket_forms/100"
    tf = payload["ticket_form"]
    # System field keeps its slot in the form's field order.
    assert tf["ticket_field_ids"] == [910, 920, 930]
    # The system-parented condition survived and was remapped end to end.
    assert "agent_conditions" in tf
    cond = tf["agent_conditions"][0]
    assert cond["parent_field_id"] == 910
    assert cond["value"] == "incident"
    assert cond["child_fields"][0]["id"] == 930
    assert cond["child_fields"][0]["required_on_statuses"] == {"type": "ALL_STATUSES"}


def test_assign_form_fields_custom_only_still_works(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_form_fields

    target_fields = [{"id": 920, "type": "tagger", "title": "Cloud"},
                     {"id": 930, "type": "integer", "title": "Acct"}]
    client = FakeFormClient(target_fields)
    id_map = {
        "ticket_forms": {"1": "100"},
        "ticket_fields": {"20": "920", "30": "930"},
    }
    source_forms = [{
        "id": 1, "name": "Tech",
        "ticket_field_ids": [20, 30],
        "agent_conditions": [{
            "parent_field_id": 20, "value": "aws",
            "child_fields": [{"id": 30, "is_required": False}],
        }],
    }]
    source_fields = [{"id": 20, "type": "tagger"}, {"id": 30, "type": "integer"}]

    _assign_form_fields(client, id_map, source_forms, source_fields)

    tf = client.puts[0][1]["ticket_form"]
    assert tf["ticket_field_ids"] == [920, 930]
    assert tf["agent_conditions"][0]["parent_field_id"] == 920


def test_assign_form_fields_skips_unmapped_form(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _assign_form_fields

    client = FakeFormClient([])
    id_map = {"ticket_forms": {}, "ticket_fields": {"20": "920"}}
    source_forms = [{"id": 1, "name": "Tech", "ticket_field_ids": [20]}]

    _assign_form_fields(client, id_map, source_forms, [{"id": 20, "type": "tagger"}])

    assert client.puts == []  # form never mapped → nothing pushed


# ------------------------------------------------------------------ #
#  import_resource conflict_mode="skip" reconciliation              #
# ------------------------------------------------------------------ #

def test_importer_skip_records_existing_mapping(tmp_path: Path) -> None:
    """A pre-existing target resource (skip mode) must be recorded in id_map
    so dependent phases can resolve references to it — without re-creating it."""
    _fresh(tmp_path)
    from src.importer import import_resource

    client = FakeImportClient(existing=[{"id": 555, "name": "Group A"}])
    id_map: dict = {}

    import_resource(
        client=client, id_map=id_map,
        source_items=[{"id": 5, "name": "Group A"}],
        resource_key="groups",
        list_path="groups", list_rkey="groups",
        create_path="groups", create_rkey="group",
        create_response_rkey="group",
        delete_path_fn=lambda tid: f"groups/{tid}",
        conflict_mode="skip",
    )

    assert id_map.get("groups", {}).get("5") == "555"
    assert client.posts == []   # not recreated
    assert client.deletes == []  # skip mode never deletes
    _reset_importer_globals()


def test_importer_skip_creates_when_no_conflict(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.importer import import_resource

    client = FakeImportClient(existing=[])  # target empty
    id_map: dict = {}

    import_resource(
        client=client, id_map=id_map,
        source_items=[{"id": 5, "name": "Group A"}],
        resource_key="groups",
        list_path="groups", list_rkey="groups",
        create_path="groups", create_rkey="group",
        create_response_rkey="group",
        delete_path_fn=lambda tid: f"groups/{tid}",
        conflict_mode="skip",
    )

    assert len(client.posts) == 1            # created
    assert id_map.get("groups", {}).get("5") == "999"
    _reset_importer_globals()


# ------------------------------------------------------------------ #
#  Manual harness                                                     #
# ------------------------------------------------------------------ #

def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-formmap-") as td:
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
