"""
Regression test for the observability bug: import_resource's parallel CREATE
pass runs in a ThreadPoolExecutor whose worker threads did NOT inherit the
`current_migration_id` ContextVar, so per-item CREATED/FAILED events resolved
to the legacy state dir and vanished from the per-migration report + UI stream.

This asserts the run context is now visible INSIDE the pool workers.
"""

from __future__ import annotations

import os
import sys
import threading
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
    """Records the migration-id ContextVar value observed inside each
    worker thread during create()."""

    def __init__(self, seen):
        self._seen = seen

    def list_resource(self, path, rkey):
        return []  # no conflicts

    def post(self, path, payload):
        from src.utils.runctx import current_migration_id
        self._seen.append((threading.get_ident(), current_migration_id.get()))
        return {payload_key(payload): {"id": 1000 + len(self._seen)}}


def payload_key(payload):
    # the resource wrapper key used in import_resource ("group")
    return next(iter(payload))


def test_pool_workers_inherit_migration_id(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.importer import import_resource
    from src.utils.runctx import current_migration_id

    seen = []
    client = FakeClient(seen)
    # >1 item so the ThreadPoolExecutor path (not the sequential shortcut) runs.
    items = [{"id": i, "name": f"g{i}"} for i in range(5)]

    token = current_migration_id.set("testmid123")
    try:
        import_resource(
            client=client, id_map={}, source_items=items,
            resource_key="groups",
            list_path="groups", list_rkey="groups",
            create_path="groups", create_rkey="group",
            create_response_rkey="group",
            delete_path_fn=lambda t: f"groups/{t}",
        )
    finally:
        current_migration_id.reset(token)

    assert len(seen) == 5
    # At least one create ran on a NON-main thread (proves the pool was used)...
    main_id = threading.get_ident()
    assert any(tid != main_id for tid, _ in seen)
    # ...and EVERY worker saw the bound migration id, not the legacy default.
    assert all(mid == "testmid123" for _, mid in seen), seen
