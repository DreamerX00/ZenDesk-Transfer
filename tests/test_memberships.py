"""
Unit tests for membership migration (gaps #3 group memberships, #4 org
memberships) in src.phases.phase5_users.

Covers the pure pair-builder (_build_membership_pairs) and the create paths
(_migrate_group_memberships / _migrate_org_memberships) using a fake client.
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


class FakeClient:
    """Minimal stand-in for ZendeskClient covering post/list_resource/get_all."""

    def __init__(self, existing=None, fail_paths=None):
        self.dry_run = False
        self._existing = existing or {}
        self._fail_paths = set(fail_paths or [])
        self.posts = []
        self._next_id = 1000

    def list_resource(self, path, rkey):
        return self._existing.get(path, [])

    def get_all(self, path, rkey):
        """Streaming equivalent — yields items from the same backing dict."""
        yield from self._existing.get(path, [])

    def post(self, path, payload):
        self.posts.append((path, payload))
        if path in self._fail_paths:
            from src.client import ZendeskAPIError
            raise ZendeskAPIError(422, "duplicate", path)
        self._next_id += 1
        # Echo back a created resource with the singular wrapper key.
        rkey = "group_membership" if "group" in path else "organization_membership"
        body = dict(payload.get(rkey, {}))
        body["id"] = self._next_id
        return {rkey: body}


# ------------------------------------------------------------------ #
#  _build_membership_pairs                                            #
# ------------------------------------------------------------------ #

def test_build_pairs_maps_ids(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _build_membership_pairs

    src = [{"id": 1, "user_id": 5, "group_id": 7, "default": True}]
    id_map = {"users": {"5": "55"}, "groups": {"7": "77"}}

    pairs = _build_membership_pairs(
        src, id_map, user_key="user_id", ref_key="group_id",
        ref_category="groups", resource_label="group_memberships",
    )

    assert pairs == [{"user_id": 55, "group_id": 77, "default": True, "_src": 1}]


def test_build_pairs_skips_unmapped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _build_membership_pairs

    src = [
        {"id": 1, "user_id": 5, "group_id": 7},     # both mapped
        {"id": 2, "user_id": 99, "group_id": 7},    # user unmapped
        {"id": 3, "user_id": 5, "group_id": 88},    # group unmapped
    ]
    id_map = {"users": {"5": "55"}, "groups": {"7": "77"}}

    pairs = _build_membership_pairs(
        src, id_map, user_key="user_id", ref_key="group_id",
        ref_category="groups", resource_label="group_memberships",
    )

    assert len(pairs) == 1
    assert pairs[0]["_src"] == 1


def test_build_pairs_missing_categories(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _build_membership_pairs

    src = [{"id": 1, "user_id": 5, "group_id": 7}]
    # No 'users' / 'groups' categories at all → no crash, empty result.
    pairs = _build_membership_pairs(
        src, {}, user_key="user_id", ref_key="group_id",
        ref_category="groups", resource_label="group_memberships",
    )
    assert pairs == []


def test_build_pairs_skips_missing_fields(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _build_membership_pairs

    src = [{"id": 1, "user_id": None, "group_id": 7}]
    id_map = {"users": {"5": "55"}, "groups": {"7": "77"}}

    pairs = _build_membership_pairs(
        src, id_map, user_key="user_id", ref_key="group_id",
        ref_category="groups", resource_label="group_memberships",
    )
    assert pairs == []


# ------------------------------------------------------------------ #
#  _migrate_group_memberships                                         #
# ------------------------------------------------------------------ #

def test_group_memberships_created(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_group_memberships

    client = FakeClient()
    id_map = {"users": {"5": "55"}, "groups": {"7": "77"}}
    src = [{"id": 1, "user_id": 5, "group_id": 7, "default": False}]

    _migrate_group_memberships(client, id_map, src)

    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "group_memberships"
    assert payload["group_membership"] == {
        "user_id": 55, "group_id": 77, "default": False
    }


def test_group_memberships_dedup_existing(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_group_memberships

    # Target already has user 55 in group 77 → no POST should be made.
    client = FakeClient(existing={
        "group_memberships": [{"user_id": 55, "group_id": 77}]
    })
    id_map = {"users": {"5": "55"}, "groups": {"7": "77"}}
    src = [{"id": 1, "user_id": 5, "group_id": 7}]

    _migrate_group_memberships(client, id_map, src)

    assert client.posts == []


def test_group_memberships_failure_is_isolated(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_group_memberships

    client = FakeClient(fail_paths={"group_memberships"})
    id_map = {"users": {"5": "55", "6": "66"}, "groups": {"7": "77"}}
    src = [
        {"id": 1, "user_id": 5, "group_id": 7},
        {"id": 2, "user_id": 6, "group_id": 7},
    ]

    # Should not raise even though every POST 422s.
    _migrate_group_memberships(client, id_map, src)
    assert len(client.posts) == 2


def test_group_memberships_empty_noop(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_group_memberships

    client = FakeClient()
    _migrate_group_memberships(client, {}, [])
    assert client.posts == []


# ------------------------------------------------------------------ #
#  _migrate_org_memberships                                           #
# ------------------------------------------------------------------ #

def test_org_memberships_created(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_org_memberships

    client = FakeClient()
    id_map = {"users": {"5": "55"}, "organizations": {"9": "99"}}
    src = [{"id": 1, "user_id": 5, "organization_id": 9, "default": True}]

    _migrate_org_memberships(client, id_map, src)

    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "organization_memberships"
    assert payload["organization_membership"] == {
        "user_id": 55, "organization_id": 99, "default": True
    }


def test_org_memberships_dedup_existing(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _migrate_org_memberships

    client = FakeClient(existing={
        "organization_memberships": [{"user_id": 55, "organization_id": 99}]
    })
    id_map = {"users": {"5": "55"}, "organizations": {"9": "99"}}
    src = [{"id": 1, "user_id": 5, "organization_id": 9}]

    _migrate_org_memberships(client, id_map, src)
    assert client.posts == []


def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-mem-") as td:
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
