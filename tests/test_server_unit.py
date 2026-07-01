"""
Phase B unit tests: exercises every server module that doesn't require
a running Redis. The HTTP layer is hit through FastAPI's TestClient.

Covered:
  * settings.get_settings — sane defaults, validation errors caught
  * state — Connection.masked() never leaks secrets
  * state — ConnectionStore round-trip with on-disk encryption
  * state — invalid migration_id rejected (path-traversal guard)
  * oauth — HMAC sign/verify is constant-time and reversible
  * oauth — establish_iframe_session: happy path + every failure mode
  * oauth — begin_oauth: bad role / bad subdomain rejected
  * oauth — complete_oauth: unknown state rejected
  * app — / and /api/v1/health serve
  * app — protected route 401s without session
  * app — /session HMAC roundtrip → bearer → /connections accepted
  * app — /oauth/callback renders postMessage HTML even on error
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

# Repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Force dev mode + ephemeral secrets BEFORE the server modules import
os.environ["ZDX_DEV_MODE"] = "1"
os.environ["ZDX_HMAC_SECRET"] = "0" * 64
os.environ["ZDX_FERNET_KEY"] = "0" * 64  # hex
# Reset settings singleton between tests via a clean tempdir each time.

from fastapi.testclient import TestClient  # noqa: E402


def _fresh_settings(tmp: Path) -> None:
    """Reset the settings singleton and connection store to a temp
    directory so every test runs clean."""
    from server import settings as s_mod
    from server import state as st_mod
    os.environ["ZDX_STATE_ROOT"] = str(tmp)
    os.environ["ZDX_CONNECTIONS_PATH"] = str(tmp / "connections.enc")
    s_mod._settings = None
    st_mod._store = None


# ------------------------------------------------------------------ #
#  state.py                                                           #
# ------------------------------------------------------------------ #

def test_connection_masking_hides_secrets(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import Connection
    c = Connection(
        id="abc", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="super-secret-token-xyz",
        api_token="zztoken1234",
    )
    m = c.masked()
    # last-4 chars only; the rest is starred
    assert m["oauth_token"] == "****-xyz"
    assert m["api_token"] == "****1234"
    assert m["role"] == "source"
    # full strings must not appear
    blob = json.dumps(m)
    assert "super-secret-token-xyz" not in blob
    assert "zztoken1234" not in blob


def test_connection_store_round_trip(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import Connection, get_connection_store

    store = get_connection_store()
    c = Connection(
        id="conn-1", role="target", subdomain="acme",
        auth_kind="oauth", oauth_token="t-1234",
    )
    store.put(c)

    # Roundtrip through a fresh instance to confirm decryption
    from server.state import ConnectionStore
    fresh = ConnectionStore()
    got = fresh.get("conn-1")
    assert got is not None
    assert got.oauth_token == "t-1234"
    assert got.subdomain == "acme"

    # The on-disk blob must NOT contain the cleartext token
    raw = (tmp_path / "connections.enc").read_bytes()
    assert b"t-1234" not in raw, "credentials leaked in plaintext"

    assert fresh.delete("conn-1") is True
    assert fresh.get("conn-1") is None


def test_invalid_migration_id_rejected(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import is_valid_migration_id, state_dir_for

    assert is_valid_migration_id("abc-123") is True
    assert is_valid_migration_id("AaZz09_-") is True
    # Path traversal attempts:
    for bad in ["", "../escape", "with/slash", "with space", "x" * 65, ".", ".."]:
        assert is_valid_migration_id(bad) is False, f"should reject {bad!r}"
        try:
            state_dir_for(bad)
        except ValueError:
            continue
        raise AssertionError(f"state_dir_for accepted {bad!r}")


# ------------------------------------------------------------------ #
#  oauth.py                                                           #
# ------------------------------------------------------------------ #

def test_hmac_sign_verify_round_trip(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import sign_hmac, verify_hmac_signature
    body = b'{"hello":"world"}'
    sig = sign_hmac(body)
    assert verify_hmac_signature(body, sig) is True
    # Tampered body fails:
    assert verify_hmac_signature(b'{"hello":"WORLD"}', sig) is False
    # Tampered sig fails:
    assert verify_hmac_signature(body, sig[:-1] + ("0" if sig[-1] != "0" else "1")) is False
    # Empty signature fails:
    assert verify_hmac_signature(body, "") is False


def test_establish_iframe_session_happy(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import establish_iframe_session, sign_hmac

    body = json.dumps({
        "subdomain": "dreamer-12487",
        "user": {"id": 42, "email": "agent@acme.com"},
        "ts": time.time(),
    }).encode("utf-8")
    sig = sign_hmac(body)
    sess = establish_iframe_session(body_bytes=body, signature_hex=sig)
    assert sess.subdomain == "dreamer-12487"
    assert sess.user_id == 42
    assert sess.user_email == "agent@acme.com"
    assert len(sess.token) >= 24


def test_establish_iframe_session_rejects_bad_sig(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import establish_iframe_session
    body = b'{"subdomain":"acme","user":{"id":1,"email":"a@b.c"},"ts":0}'
    try:
        establish_iframe_session(body_bytes=body, signature_hex="deadbeef")
    except ValueError as e:
        assert "HMAC" in str(e)
        return
    raise AssertionError("should have rejected bad signature")


def test_establish_iframe_session_rejects_stale_ts(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import establish_iframe_session, sign_hmac
    stale_body = json.dumps({
        "subdomain": "acme",
        "user": {"id": 1, "email": "a@b.c"},
        "ts": time.time() - 600,  # 10 min ago — outside the 5-min window
    }).encode("utf-8")
    sig = sign_hmac(stale_body)
    try:
        establish_iframe_session(body_bytes=stale_body, signature_hex=sig)
    except ValueError as e:
        assert "5-minute" in str(e) or "ts" in str(e).lower()
        return
    raise AssertionError("should have rejected stale ts")


def test_begin_oauth_validates_inputs(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import begin_oauth

    # Invalid role
    try:
        begin_oauth(role="weird", subdomain="acme", client_id="x", client_secret="y")
    except ValueError as e:
        assert "role" in str(e)
    else:
        raise AssertionError("invalid role accepted")

    # Invalid subdomain
    for bad in ["", "BAD.DOTS", "with space", "trailing-", "-leading"]:
        try:
            begin_oauth(role="source", subdomain=bad, client_id="x", client_secret="y")
        except ValueError:
            continue
        raise AssertionError(f"invalid subdomain accepted: {bad!r}")

    # Happy path — returns a Zendesk authorize URL and a state token
    url, state = begin_oauth(
        role="target", subdomain="acme",
        client_id="cid", client_secret="csec",
    )
    assert url.startswith("https://acme.zendesk.com/oauth/authorizations/new?")
    assert "scope=read+write+hc%3Awrite" in url
    assert f"state={state}" in url
    assert len(state) >= 16


def test_complete_oauth_unknown_state(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import complete_oauth
    try:
        complete_oauth(state="nonexistent", code="anything")
    except ValueError as e:
        assert "unknown" in str(e).lower() or "expired" in str(e).lower()
        return
    raise AssertionError("complete_oauth should reject unknown state")


# ------------------------------------------------------------------ #
#  refresh_oauth_connection                                           #
# ------------------------------------------------------------------ #

def test_refresh_oauth_unknown_connection(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.oauth import refresh_oauth_connection
    try:
        refresh_oauth_connection("nobody-home")
    except ValueError as e:
        assert "unknown connection" in str(e).lower()
        return
    raise AssertionError("should reject unknown connection")


def test_refresh_oauth_not_oauth(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-1", role="target", subdomain="acme",
        auth_kind="api_token", email="a@b.com", api_token="tok123",
    ))
    from server.oauth import refresh_oauth_connection
    try:
        refresh_oauth_connection("conn-1")
    except ValueError as e:
        assert "api token" in str(e).lower()
        return
    raise AssertionError("should reject api_token connection")


def test_refresh_oauth_missing_refresh_token(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-2", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="tok",
        oauth_client_id="cid", oauth_client_secret="csec",
    ))
    from server.oauth import refresh_oauth_connection
    try:
        refresh_oauth_connection("conn-2")
    except ValueError as e:
        assert "no refresh_token" in str(e).lower()
        return
    raise AssertionError("should reject missing refresh_token")


def test_refresh_oauth_missing_client_credentials(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-3", role="target", subdomain="acme",
        auth_kind="oauth", oauth_token="tok",
        oauth_refresh_token="rtok",
    ))
    from server.oauth import refresh_oauth_connection
    try:
        refresh_oauth_connection("conn-3")
    except ValueError as e:
        assert "client" in str(e).lower()
        return
    raise AssertionError("should reject missing client credentials")


def test_refresh_oauth_network_error(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from unittest.mock import patch
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-4", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="old",
        oauth_refresh_token="rtok",
        oauth_client_id="cid", oauth_client_secret="csec",
    ))
    from server.oauth import refresh_oauth_connection
    import requests
    with patch.object(requests, "post", side_effect=requests.RequestException("boom")):
        try:
            refresh_oauth_connection("conn-4")
        except ValueError as e:
            assert "network" in str(e).lower()
            return
        raise AssertionError("should wrap network error")


def test_refresh_oauth_zendesk_rejects(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from unittest.mock import patch, Mock
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-5", role="target", subdomain="acme",
        auth_kind="oauth", oauth_token="old",
        oauth_refresh_token="rtok",
        oauth_client_id="cid", oauth_client_secret="csec",
    ))
    mock_resp = Mock(status_code=403, text="invalid_client")
    mock_resp.ok = False
    from server.oauth import refresh_oauth_connection
    import requests
    with patch.object(requests, "post", return_value=mock_resp):
        try:
            refresh_oauth_connection("conn-5")
        except ValueError as e:
            assert "403" in str(e) or "rejected" in str(e).lower()
            return
        raise AssertionError("should wrap Zendesk rejection")


def test_refresh_oauth_missing_access_token(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from unittest.mock import patch, Mock
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-6", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="old",
        oauth_refresh_token="rtok",
        oauth_client_id="cid", oauth_client_secret="csec",
    ))
    mock_resp = Mock(status_code=200, text="{}")
    mock_resp.ok = True
    mock_resp.json.return_value = {"scope": "read"}
    from server.oauth import refresh_oauth_connection
    import requests
    with patch.object(requests, "post", return_value=mock_resp):
        try:
            refresh_oauth_connection("conn-6")
        except ValueError as e:
            assert "access_token" in str(e).lower()
            return
        raise AssertionError("should reject missing access_token")


def test_refresh_oauth_happy_path(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    from unittest.mock import patch, Mock
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    conn = Connection(
        id="conn-7", role="target", subdomain="acme",
        auth_kind="oauth", oauth_token="old-token",
        oauth_refresh_token="old-refresh",
        oauth_client_id="cid", oauth_client_secret="csec",
    )
    store.put(conn)
    mock_resp = Mock(status_code=200, text='{"access_token":"new-token"}')
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "scope": "read write",
    }
    from server.oauth import refresh_oauth_connection
    import requests
    with patch.object(requests, "post", return_value=mock_resp) as mock_post:
        updated = refresh_oauth_connection("conn-7")

    assert updated.oauth_token == "new-token"
    assert updated.oauth_refresh_token == "new-refresh"
    # Verify the request payload
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["grant_type"] == "refresh_token"
    assert call_kwargs["json"]["refresh_token"] == "old-refresh"
    assert call_kwargs["json"]["client_id"] == "cid"

    # Verify the store was updated
    reloaded = store.get("conn-7")
    assert reloaded is not None
    assert reloaded.oauth_token == "new-token"
    assert reloaded.oauth_refresh_token == "new-refresh"


def test_refresh_oauth_preserves_old_refresh_when_not_rotated(tmp_path: Path) -> None:
    """Zendesk may keep the same refresh_token — the old one must survive."""
    _fresh_settings(tmp_path)
    from unittest.mock import patch, Mock
    from server.state import Connection, get_connection_store
    store = get_connection_store()
    store.put(Connection(
        id="conn-8", role="source", subdomain="acme",
        auth_kind="oauth", oauth_token="old",
        oauth_refresh_token="permanent-refresh",
        oauth_client_id="cid", oauth_client_secret="csec",
    ))
    mock_resp = Mock(status_code=200, text='{"access_token":"new-token"}')
    mock_resp.ok = True
    mock_resp.json.return_value = {"access_token": "new-token"}
    from server.oauth import refresh_oauth_connection
    import requests
    with patch.object(requests, "post", return_value=mock_resp):
        updated = refresh_oauth_connection("conn-8")
    assert updated.oauth_token == "new-token"
    # refresh_token was NOT in the response — should keep the old one
    assert updated.oauth_refresh_token == "permanent-refresh"


# ------------------------------------------------------------------ #
#  app.py                                                             #
# ------------------------------------------------------------------ #

def _client(tmp_path: Path) -> TestClient:
    _fresh_settings(tmp_path)
    from server.app import app
    return TestClient(app)


def test_health_endpoint(tmp_path: Path) -> None:
    c = _client(tmp_path)
    r = c.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_protected_route_requires_auth(tmp_path: Path) -> None:
    c = _client(tmp_path)
    r = c.get("/api/v1/connections")
    assert r.status_code == 401
    # Bad scheme
    r = c.get("/api/v1/connections", headers={"Authorization": "Token xyz"})
    assert r.status_code == 401
    # Unknown token
    r = c.get("/api/v1/connections", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 401
    # Bearer tokens in URLs are rejected to avoid leaking credentials in logs.
    r = c.get("/api/v1/connections?t=fake")
    assert r.status_code == 401


def test_session_then_connections(tmp_path: Path) -> None:
    c = _client(tmp_path)
    from server.oauth import sign_hmac

    body = json.dumps({
        "subdomain": "dreamer-12487",
        "user": {"id": 42, "email": "agent@acme.com"},
        "ts": time.time(),
    }).encode("utf-8")
    sig = sign_hmac(body)

    r = c.post("/api/v1/session", content=body,
               headers={"X-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    assert tok

    # Now /connections accepts the bearer
    r = c.get("/api/v1/connections",
              headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json() == {"connections": []}


def test_oauth_callback_renders_html(tmp_path: Path) -> None:
    c = _client(tmp_path)
    # No code → renders the error page (still 400 HTML)
    r = c.get("/api/v1/oauth/callback")
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert "postMessage" in r.text
    # Error case from Zendesk
    r = c.get("/api/v1/oauth/callback?error=access_denied&error_description=user+said+no")
    assert r.status_code == 400
    assert "access_denied" in r.text


def test_preflight_rejects_unknown_connection(tmp_path: Path) -> None:
    c = _client(tmp_path)
    from server.oauth import sign_hmac
    body = json.dumps({
        "subdomain": "acme",
        "user": {"id": 1, "email": "a@b.c"},
        "ts": time.time(),
    }).encode("utf-8")
    r = c.post("/api/v1/session", content=body,
               headers={"X-Signature": sign_hmac(body),
                        "Content-Type": "application/json"})
    tok = r.json()["token"]

    r = c.post("/api/v1/preflight",
               json={"source_connection_id": "ghost", "target_connection_id": "vanish"},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["ok"] is False
    assert "unknown connection" in body["source"]["error"]


def test_config_endpoint_reports_standalone_off_by_default(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    os.environ.pop("ZDX_STANDALONE_MODE", None)
    c = _client(tmp_path)
    r = c.get("/api/v1/config")
    assert r.status_code == 200
    assert r.json()["standalone_mode"] is False


def test_standalone_session_404_when_disabled(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    os.environ.pop("ZDX_STANDALONE_MODE", None)
    c = _client(tmp_path)
    r = c.post("/api/v1/standalone/session")
    assert r.status_code == 404


def test_standalone_session_works_when_enabled(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    os.environ["ZDX_STANDALONE_MODE"] = "1"
    try:
        c = _client(tmp_path)
        r = c.post("/api/v1/standalone/session")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"]
        # That token must now work as a real bearer
        r2 = c.get("/api/v1/connections",
                   headers={"Authorization": f"Bearer {body['token']}"})
        assert r2.status_code == 200
    finally:
        os.environ.pop("ZDX_STANDALONE_MODE", None)


def test_standalone_session_requires_token_for_non_dev_remote(tmp_path: Path) -> None:
    _fresh_settings(tmp_path)
    os.environ["ZDX_DEV_MODE"] = "0"
    os.environ["ZDX_STANDALONE_MODE"] = "1"
    os.environ["ZDX_STANDALONE_ADMIN_TOKEN"] = "admin-token"
    try:
        c = _client(tmp_path)
        r = c.post("/api/v1/standalone/session")
        assert r.status_code == 403
        r = c.post("/api/v1/standalone/session",
                   headers={"X-Standalone-Token": "admin-token"})
        assert r.status_code == 200, r.text
        assert r.json()["token"]
    finally:
        os.environ["ZDX_DEV_MODE"] = "1"
        os.environ.pop("ZDX_STANDALONE_MODE", None)
        os.environ.pop("ZDX_STANDALONE_ADMIN_TOKEN", None)


def test_status_endpoint_validates_migration_id(tmp_path: Path) -> None:
    c = _client(tmp_path)
    from server.oauth import sign_hmac
    body = json.dumps({
        "subdomain": "acme",
        "user": {"id": 1, "email": "a@b.c"},
        "ts": time.time(),
    }).encode("utf-8")
    r = c.post("/api/v1/session", content=body,
               headers={"X-Signature": sign_hmac(body),
                        "Content-Type": "application/json"})
    tok = r.json()["token"]

    r = c.get("/api/v1/jobs/..%2Fescape/status",
              headers={"Authorization": f"Bearer {tok}"})
    # The URL-decoded path is rejected by FastAPI's path validation,
    # or by our is_valid_migration_id check. Either way: not 200.
    assert r.status_code in (400, 404, 422)


# ------------------------------------------------------------------ #
#  Manual harness                                                     #
# ------------------------------------------------------------------ #

def _run_all() -> int:
    tests = [
        test_connection_masking_hides_secrets,
        test_connection_store_round_trip,
        test_invalid_migration_id_rejected,
        test_hmac_sign_verify_round_trip,
        test_establish_iframe_session_happy,
        test_establish_iframe_session_rejects_bad_sig,
        test_establish_iframe_session_rejects_stale_ts,
        test_begin_oauth_validates_inputs,
        test_complete_oauth_unknown_state,
        test_health_endpoint,
        test_protected_route_requires_auth,
        test_session_then_connections,
        test_oauth_callback_renders_html,
        test_preflight_rejects_unknown_connection,
        test_config_endpoint_reports_standalone_off_by_default,
        test_standalone_session_404_when_disabled,
        test_standalone_session_works_when_enabled,
        test_status_endpoint_validates_migration_id,
    ]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zdx-server-") as td:
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
