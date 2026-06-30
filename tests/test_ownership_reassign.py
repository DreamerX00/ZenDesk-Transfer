"""
Unit tests for deferred ownership reassignment (gap #5) in
src.phases.phase5_users:

  - _reassign_view_owners       (personal view owner_id)
  - _reassign_article_authors   (HC article author_id)

Both run in Phase 5 after users exist, reassigning ownership only when the
owner/author user actually migrated.
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
    def __init__(self, fail=False):
        self.dry_run = False
        self.puts = []
        self._fail = fail

    def put(self, path, payload):
        self.puts.append((path, payload))
        if self._fail:
            from src.client import ZendeskAPIError
            raise ZendeskAPIError(404, "not found", path)
        return {"ok": True}


# ------------------------------------------------------------------ #
#  _reassign_view_owners                                              #
# ------------------------------------------------------------------ #

def test_view_owner_reassigned(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient()
    id_map = {"views": {"1": "100"}, "users": {"5": "55"}}
    views = [{"id": 1, "title": "My View", "owner_id": 5}]

    _reassign_view_owners(client, id_map, views)

    assert client.puts == [("views/100", {"view": {"owner_id": 55}})]


def test_view_owner_unmapped_user_is_manual(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient()
    # View migrated but its owner (user 99) did not.
    id_map = {"views": {"1": "100"}, "users": {"5": "55"}}
    views = [{"id": 1, "title": "Orphan", "owner_id": 99}]

    _reassign_view_owners(client, id_map, views)

    assert client.puts == []  # no reassignment attempted


def test_view_without_owner_skipped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient()
    id_map = {"views": {"1": "100"}, "users": {"5": "55"}}
    views = [{"id": 1, "title": "Shared"}]  # no owner_id

    _reassign_view_owners(client, id_map, views)
    assert client.puts == []


def test_view_not_migrated_skipped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient()
    # owner mapped, but the view itself wasn't migrated.
    id_map = {"views": {}, "users": {"5": "55"}}
    views = [{"id": 1, "title": "V", "owner_id": 5}]

    _reassign_view_owners(client, id_map, views)
    assert client.puts == []


def test_view_put_failure_isolated(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient(fail=True)
    id_map = {"views": {"1": "100"}, "users": {"5": "55"}}
    views = [{"id": 1, "title": "V", "owner_id": 5}]

    # Must not raise.
    _reassign_view_owners(client, id_map, views)
    assert len(client.puts) == 1


def test_view_missing_categories_noop(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_view_owners

    client = FakeClient()
    _reassign_view_owners(client, {}, [{"id": 1, "owner_id": 5}])
    assert client.puts == []


# ------------------------------------------------------------------ #
#  _reassign_article_authors                                          #
# ------------------------------------------------------------------ #

def test_article_author_reassigned(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_article_authors

    client = FakeClient()
    id_map = {"hc_articles": {"1": "100"}, "users": {"5": "55"}}
    articles = [{"id": 1, "title": "Doc", "author_id": 5}]

    _reassign_article_authors(client, id_map, articles)

    assert client.puts == [
        ("help_center/articles/100", {"article": {"author_id": 55}})
    ]


def test_article_author_unmapped_is_manual(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_article_authors

    client = FakeClient()
    id_map = {"hc_articles": {"1": "100"}, "users": {"5": "55"}}
    articles = [{"id": 1, "title": "Doc", "author_id": 99}]

    _reassign_article_authors(client, id_map, articles)
    assert client.puts == []


def test_article_without_author_skipped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase5_users import _reassign_article_authors

    client = FakeClient()
    id_map = {"hc_articles": {"1": "100"}, "users": {"5": "55"}}
    articles = [{"id": 1, "title": "Doc"}]

    _reassign_article_authors(client, id_map, articles)
    assert client.puts == []


def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-own-") as td:
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
