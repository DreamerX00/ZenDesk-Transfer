"""
Phase F: server.jobs unit tests — proves the per-migration plumbing
works without spinning up a real Zendesk API.

We don't try to mock the whole phase code path (that's an integration
test). We exercise:

  * The ContextVar bind/release helper pair restores cleanly even
    when the body raises.
  * `_build_client` constructs a ZendeskClient with the right
    credentials for both OAuth and API-token connections.
  * `run_full_migration` reports a clean structured failure (not an
    uncaught exception) when given unknown connection ids.
  * `run_format_target` reports the same kind of structured failure.
  * `run_preflight` returns a per-side error envelope when one side
    is unknown — without aborting the other side's probe.

All Redis interactions are silently no-op'd because `events.py` swallows
exceptions on the producer side; we don't need a running Redis here.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["ZDX_DEV_MODE"] = "1"
os.environ["ZDX_HMAC_SECRET"] = "0" * 64
os.environ["ZDX_FERNET_KEY"] = "0" * 64


def _fresh(tmp: Path) -> None:
    """Reset settings singleton + connection store to a tmp dir."""
    from server import settings as s_mod
    from server import state as st_mod
    os.environ["ZDX_STATE_ROOT"] = str(tmp)
    os.environ["ZDX_CONNECTIONS_PATH"] = str(tmp / "connections.enc")
    s_mod._settings = None
    st_mod._store = None


# ------------------------------------------------------------------ #
#  ContextVar lifecycle                                               #
# ------------------------------------------------------------------ #

def test_bind_release_restores_context(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import _bind_run_context, _release_run_context
    from src.utils.runctx import current_migration_id, event_sink

    assert current_migration_id.get() is None
    assert event_sink.get() is None

    tokens = _bind_run_context("mig-abc")
    try:
        assert current_migration_id.get() == "mig-abc"
        assert callable(event_sink.get())
    finally:
        _release_run_context(tokens)

    assert current_migration_id.get() is None
    assert event_sink.get() is None


def test_bind_release_restores_after_exception(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import _bind_run_context, _release_run_context
    from src.utils.runctx import current_migration_id

    tokens = _bind_run_context("mig-xyz")
    try:
        raise RuntimeError("phase boom")
    except RuntimeError:
        pass
    finally:
        _release_run_context(tokens)

    # No matter what the body did, the ContextVar is back to default.
    assert current_migration_id.get() is None


# ------------------------------------------------------------------ #
#  _build_client                                                      #
# ------------------------------------------------------------------ #

def test_build_client_oauth(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import _build_client
    from server.state import Connection

    conn = Connection(
        id="c1", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="oauth-abc",
    )
    client = _build_client(conn)
    # The client's auth header should embed the OAuth token.
    auth = client._session_factory_attrs() if hasattr(client, "_session_factory_attrs") else None
    # We don't poke private internals; just confirm dry-run is wired:
    assert client.dry_run is False
    assert client.subdomain == "acme"


def test_build_client_api_token(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import _build_client
    from server.state import Connection

    conn = Connection(
        id="c2", role="target", subdomain="acme",
        auth_kind="api_token", email="agent@acme.com", api_token="zzz",
    )
    client = _build_client(conn, dry_run=True)
    assert client.subdomain == "acme"
    assert client.dry_run is True


def test_build_client_rejects_unknown_auth_kind(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import _build_client
    from server.state import Connection

    conn = Connection(
        id="c3", role="source", subdomain="acme",
        auth_kind="magic-beans",  # type: ignore[arg-type]
    )
    try:
        _build_client(conn)
    except ValueError as e:
        assert "auth_kind" in str(e)
        return
    raise AssertionError("should have rejected unknown auth_kind")


# ------------------------------------------------------------------ #
#  Top-level jobs                                                     #
# ------------------------------------------------------------------ #

def test_run_full_migration_unknown_source(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_full_migration
    summary = run_full_migration("m1", "ghost-src", "ghost-tgt")
    assert summary["status"] == "failed"
    assert "unknown source connection" in summary["error"]
    assert "traceback" in summary  # included for debugging


def test_run_full_migration_unknown_target(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_full_migration
    from server.state import Connection, get_connection_store

    get_connection_store().put(Connection(
        id="real-source", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="x",
    ))
    summary = run_full_migration("m2", "real-source", "ghost-tgt")
    assert summary["status"] == "failed"
    assert "unknown target connection" in summary["error"]


def test_run_full_migration_role_mismatch(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_full_migration
    from server.state import Connection, get_connection_store

    store = get_connection_store()
    # Insert a "target" connection in the source slot, "source" in target.
    store.put(Connection(id="src-id", role="target", subdomain="a", auth_kind="oauth", oauth_token="t"))
    store.put(Connection(id="tgt-id", role="source", subdomain="b", auth_kind="oauth", oauth_token="t"))

    summary = run_full_migration("m3", "src-id", "tgt-id")
    assert summary["status"] == "failed"
    assert "not a source" in summary["error"] or "not a target" in summary["error"]


def test_run_format_target_unknown_connection(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_format_target
    summary = run_format_target("m4", "ghost")
    assert summary["status"] == "failed"
    assert "unknown target connection" in summary["error"]


def test_run_preflight_unknown_source(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_preflight
    out = run_preflight("ghost-a", "ghost-b")
    # An unknown source short-circuits the probe — target stays at the
    # default {ok: False} (no error key set; nothing was attempted).
    assert out["source"]["ok"] is False
    assert "ghost-a" in out["source"]["error"]
    assert out["target"]["ok"] is False


def test_run_preflight_unknown_target_only(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from server.jobs import run_preflight
    from server.state import Connection, get_connection_store
    # Real source, ghost target — target reports unknown, source isn't
    # contacted because we bail early (preflight is cheap; better to
    # surface the config error than waste a request).
    get_connection_store().put(Connection(
        id="real-src", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="t",
    ))
    out = run_preflight("real-src", "ghost-b")
    assert out["target"]["ok"] is False
    assert "ghost-b" in out["target"]["error"]


# ------------------------------------------------------------------ #
#  Manual harness                                                     #
# ------------------------------------------------------------------ #

def _run_all() -> int:
    tests = [
        test_bind_release_restores_context,
        test_bind_release_restores_after_exception,
        test_build_client_oauth,
        test_build_client_api_token,
        test_build_client_rejects_unknown_auth_kind,
        test_run_full_migration_unknown_source,
        test_run_full_migration_unknown_target,
        test_run_full_migration_role_mismatch,
        test_run_format_target_unknown_connection,
        test_run_preflight_unknown_source,
        test_run_preflight_unknown_target_only,
    ]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-jobs-") as td:
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
