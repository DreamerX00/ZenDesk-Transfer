"""
server.oauth — Zendesk OAuth helpers.

Two flows live here:

  1. **Source / target connection setup** — popup flow:
       /api/v1/oauth/start  → returns the Zendesk authorize URL the
                              iframe opens in a popup
       /api/v1/oauth/callback → Zendesk redirects here with ?code; we
                                exchange for a bearer token and persist
                                it in the encrypted ConnectionStore.
       The callback HTML uses window.opener.postMessage() to notify the
       iframe and then self-closes.

  2. **Iframe session establishment** — HMAC verify:
       The iframe POSTs ZAFClient.context() to /api/v1/session, signed
       with the HMAC secret set in manifest parameters. The backend
       verifies the signature, mints an opaque session cookie, and the
       iframe carries that cookie on subsequent calls.

This module owns the cryptographic primitives. The HTTP routing is in
server.app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import requests

from server.settings import get_settings
from server.state import Connection, get_connection_store, new_migration_id


# Re-use migration_id slug shape for OAuth `state` and session ids
def _new_token() -> str:
    return secrets.token_urlsafe(24)


# ------------------------------------------------------------------ #
#  HMAC verification (iframe → backend)                               #
# ------------------------------------------------------------------ #

def verify_hmac_signature(payload: bytes, signature_hex: str) -> bool:
    """Verify an HMAC-SHA256 hex signature against the configured secret.
    Constant-time comparison. Empty signature → False."""
    if not signature_hex:
        return False
    secret = get_settings().hmac_secret.encode("utf-8")
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_hex)


def sign_hmac(payload: bytes) -> str:
    """Sign `payload` with the configured HMAC secret. Useful for tests
    and for the install-time provisioning script."""
    secret = get_settings().hmac_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ #
#  Pending OAuth flows                                                #
# ------------------------------------------------------------------ #

@dataclass
class PendingOAuth:
    state: str                    # the `state` parameter Zendesk echoes back
    role: str                     # "source" or "target"
    subdomain: str
    client_id: str
    client_secret: str
    redirect_uri: str
    requested_scope: str
    created_ts: float = field(default_factory=time.time)

    def is_expired(self, ttl_seconds: int = 600) -> bool:
        return (time.time() - self.created_ts) > ttl_seconds


class _PendingStore:
    """In-memory store for OAuth flows in flight. State is opaque and
    short-lived (10 min default), so persistence isn't required. Lost
    state after a backend restart simply means the operator must
    restart the connect flow — same effect as the popup being closed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: Dict[str, PendingOAuth] = {}

    def put(self, p: PendingOAuth) -> None:
        with self._lock:
            self._d[p.state] = p
            # Lazy GC: drop expired entries on every put.
            for k in list(self._d):
                if self._d[k].is_expired():
                    del self._d[k]

    def pop(self, state: str) -> Optional[PendingOAuth]:
        with self._lock:
            return self._d.pop(state, None)


_pending = _PendingStore()


# ------------------------------------------------------------------ #
#  Session store (iframe → backend)                                   #
# ------------------------------------------------------------------ #

@dataclass
class IframeSession:
    """A verified iframe session — issued after a successful HMAC
    handshake. Carries the target subdomain/user so subsequent API
    calls can confirm we're still operating on the right tenant."""

    token: str
    subdomain: str
    user_id: int
    user_email: str
    created_ts: float = field(default_factory=time.time)

    def is_expired(self, ttl_seconds: int = 12 * 3600) -> bool:
        return (time.time() - self.created_ts) > ttl_seconds


class _SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: Dict[str, IframeSession] = {}

    def put(self, s: IframeSession) -> None:
        with self._lock:
            self._d[s.token] = s
            for k, v in list(self._d.items()):
                if v.is_expired():
                    del self._d[k]

    def get(self, token: str) -> Optional[IframeSession]:
        with self._lock:
            s = self._d.get(token)
            if s and s.is_expired():
                del self._d[token]
                return None
            return s

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._d.pop(token, None) is not None


_sessions = _SessionStore()


# ------------------------------------------------------------------ #
#  OAuth start                                                        #
# ------------------------------------------------------------------ #

def begin_oauth(
    *,
    role: str,
    subdomain: str,
    client_id: str,
    client_secret: str,
    scope: Optional[str] = None,
    flow_mode: str = "manual",
) -> Tuple[str, str]:
    """Initiate an OAuth authorization-code flow against
    `<subdomain>.zendesk.com`. Returns `(authorize_url, state)`.

    Scope defaults match the CLI's get_oauth_token.py: read-only for
    source, full read/write/hc:write for target. The caller can
    override.

    flow_mode:
      * "manual" (CLI-parity): redirect_uri = http://localhost/callback.
        Browser hits a "localhost refused to connect" page after Allow;
        user copies the full URL back into the wizard. Matches the
        already-registered OAuth client config and adds zero attack
        surface to our backend.
      * "callback": redirect_uri points back at this backend's
        /api/v1/oauth/callback. Requires the operator to have added
        that URL to the OAuth client's redirect-URI allowlist in
        Zendesk. Kept for the popup-style flow if anyone wants it.
    """
    if role not in ("source", "target"):
        raise ValueError(f"role must be 'source' or 'target', got {role!r}")
    if not _looks_like_subdomain(subdomain):
        raise ValueError(f"invalid subdomain: {subdomain!r}")
    if flow_mode not in ("manual", "callback"):
        raise ValueError(f"flow_mode must be 'manual' or 'callback', got {flow_mode!r}")

    requested_scope = scope or _default_scope(role)
    state = _new_token()
    if flow_mode == "manual":
        # Matches the registered OAuth client redirect URI in Zendesk
        # ("http://localhost/callback") used by get_oauth_token.py.
        # The browser shows "this site can't be reached" after Allow,
        # which is intentional — the operator copies the URL.
        redirect_uri = "http://localhost/callback"
    else:
        redirect_uri = f"{get_settings().backend_url}/api/v1/oauth/callback"

    _pending.put(PendingOAuth(
        state=state,
        role=role,
        subdomain=subdomain,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        requested_scope=requested_scope,
    ))

    from urllib.parse import urlencode

    qs = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": requested_scope,
        "state": state,
    })
    return (
        f"https://{subdomain}.zendesk.com/oauth/authorizations/new?{qs}",
        state,
    )


def _default_scope(role: str) -> str:
    """Match the scopes used by get_oauth_token.py."""
    if role == "source":
        return "read"
    return "read write hc:write"


def _looks_like_subdomain(s: str) -> bool:
    import re
    # Zendesk subdomains: lowercase alphanumeric + hyphens, no dots.
    return bool(s) and bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s))


# ------------------------------------------------------------------ #
#  OAuth callback — exchange code for token                           #
# ------------------------------------------------------------------ #

def parse_redirect_url(url: str) -> Tuple[str, str]:
    """Parse a pasted Zendesk OAuth redirect URL and return
    `(code, state)`. Raises ValueError with an actionable message if
    the URL is malformed, denied, or missing required params.

    Mirrors the CLI's get_oauth_token.py parsing so the iframe wizard
    surfaces the same errors a terminal operator would see.
    """
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a parseable URL: {exc}") from None
    if not parsed.query:
        raise ValueError(
            "URL has no query string. Make sure you copied the full "
            "redirect URL from the browser's address bar — it should "
            "look like 'http://localhost/callback?code=...&state=...'."
        )
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        err = params["error"][0]
        desc = params.get("error_description", [""])[0]
        raise ValueError(f"Zendesk denied the authorization: {err} — {desc}")

    code_list = params.get("code")
    state_list = params.get("state")
    if not code_list:
        raise ValueError(
            "URL has no 'code' query parameter. Did you copy the URL "
            "BEFORE clicking Allow in Zendesk?"
        )
    if not state_list:
        raise ValueError(
            "URL has no 'state' query parameter. This usually means "
            "the URL came from a different OAuth attempt. Restart the "
            "Connect flow and copy the new URL."
        )
    return code_list[0], state_list[0]


def complete_oauth(*, state: str, code: str) -> Connection:
    """Look up the pending flow by `state`, exchange `code` for a
    bearer token, persist a Connection, and return it.

    Raises ValueError if the state is unknown/expired, or if the
    token exchange fails.
    """
    pending = _pending.pop(state)
    if pending is None:
        raise ValueError("OAuth state is unknown or has expired. Restart the connect flow.")
    if pending.is_expired():
        raise ValueError("OAuth state has expired. Restart the connect flow.")

    token_url = f"https://{pending.subdomain}.zendesk.com/oauth/tokens"
    resp = requests.post(
        token_url,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": pending.client_id,
            "client_secret": pending.client_secret,
            "redirect_uri": pending.redirect_uri,
            "scope": pending.requested_scope,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        # Don't leak the client secret or the code into the error.
        snippet = (resp.text or "")[:200]
        raise ValueError(
            f"Token exchange failed (HTTP {resp.status_code}): {snippet}"
        )
    body = resp.json()

    access_token = body.get("access_token")
    if not access_token:
        raise ValueError("Token exchange response missing 'access_token'.")

    granted_scope = body.get("scope", "")
    if pending.requested_scope and granted_scope:
        # Warn but don't abort — Zendesk sometimes downgrades unsupported
        # scopes silently. The downstream HTTP call will fail loudly
        # if a needed permission is actually missing.
        req = set(pending.requested_scope.split())
        got = set(granted_scope.split())
        missing = req - got
        if missing:
            import sys
            print(
                f"WARNING: Zendesk downgraded the OAuth scope; "
                f"missing: {sorted(missing)}. Some operations may fail.",
                file=sys.stderr,
            )

    conn = Connection(
        id=new_migration_id(),  # 12-char slug doubles as connection id
        role=pending.role,
        subdomain=pending.subdomain,
        auth_kind="oauth",
        oauth_token=access_token,
        oauth_refresh_token=body.get("refresh_token"),
        oauth_client_id=pending.client_id,
        oauth_client_secret=pending.client_secret,
    )
    get_connection_store().put(conn)
    return conn


def refresh_oauth_connection(conn_id: str) -> Connection:
    """Refresh a stored OAuth connection's access token using its
    refresh_token + client credentials.

    Mirrors the auto-refresh path in src/client.py:_refresh_token so a
    manual UI button and the migration worker hit the same endpoint
    with the same payload.

    Raises ValueError with operator-readable messages on:
      - unknown connection_id
      - connection is api_token (no OAuth refresh possible)
      - missing refresh_token / client_id / client_secret on the record
      - Zendesk rejects the refresh (expired refresh_token, revoked
        client, etc.)
    """
    store = get_connection_store()
    conn = store.get(conn_id)
    if conn is None:
        raise ValueError(f"unknown connection: {conn_id}")
    if conn.auth_kind != "oauth":
        raise ValueError(
            "This connection uses an API token, not OAuth — there is "
            "nothing to refresh. Replace the token via Direct token or "
            "Upload .env."
        )
    if not conn.oauth_refresh_token:
        raise ValueError(
            "No refresh_token was issued for this connection. Recreate "
            "the OAuth connection from the source tenant to obtain one."
        )
    if not conn.oauth_client_id or not conn.oauth_client_secret:
        raise ValueError(
            "OAuth client credentials are missing on this stored "
            "connection. Recreate it via the OAuth form so the client "
            "id / secret are captured."
        )

    token_url = f"https://{conn.subdomain}.zendesk.com/oauth/tokens"
    try:
        resp = requests.post(
            token_url,
            json={
                "grant_type": "refresh_token",
                "refresh_token": conn.oauth_refresh_token,
                "client_id": conn.oauth_client_id,
                "client_secret": conn.oauth_client_secret,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ValueError(f"Network error talking to Zendesk: {exc}") from exc

    if resp.status_code != 200:
        # Don't echo the refresh_token or client secret back.
        snippet = (resp.text or "")[:200]
        raise ValueError(
            f"Zendesk rejected the token refresh (HTTP {resp.status_code}): "
            f"{snippet}"
        )

    body = resp.json()
    new_access = body.get("access_token")
    if not new_access:
        raise ValueError("Refresh response missing 'access_token'.")

    # Zendesk may or may not rotate the refresh_token. Keep the old one
    # if a new one wasn't issued — losing it would brick future refreshes.
    new_refresh = body.get("refresh_token") or conn.oauth_refresh_token

    conn.oauth_token = new_access
    conn.oauth_refresh_token = new_refresh
    store.put(conn)
    return conn


# ------------------------------------------------------------------ #
#  Iframe session establishment                                       #
# ------------------------------------------------------------------ #

def establish_iframe_session(
    *,
    body_bytes: bytes,
    signature_hex: str,
) -> IframeSession:
    """Verify the HMAC, parse the ZAFClient.context() envelope, and
    mint a session. Raises ValueError on failure.

    Expected body shape:
        {
          "subdomain": "<target>",
          "user": {"id": 123, "email": "agent@..."},
          "ts": 1234567890
        }
    """
    if get_settings().require_hmac and not verify_hmac_signature(body_bytes, signature_hex):
        raise ValueError("invalid HMAC signature")
    try:
        env = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed session body: {exc}")

    subdomain = (env.get("subdomain") or "").strip().lower()
    user = env.get("user") or {}
    user_id = user.get("id")
    user_email = (user.get("email") or "").strip()
    ts = env.get("ts")

    if not _looks_like_subdomain(subdomain):
        raise ValueError("session body: invalid subdomain")
    if not isinstance(user_id, int):
        raise ValueError("session body: user.id must be an int")
    if "@" not in user_email:
        raise ValueError("session body: user.email is required")
    if not isinstance(ts, (int, float)):
        raise ValueError("session body: ts must be a number")
    if abs(time.time() - float(ts)) > 300:
        # Reject signatures more than 5 minutes off — guards against
        # someone replaying a captured signed envelope much later.
        raise ValueError("session body: ts is outside the 5-minute window")

    sess = IframeSession(
        token=_new_token(),
        subdomain=subdomain,
        user_id=user_id,
        user_email=user_email,
    )
    _sessions.put(sess)
    return sess


def get_iframe_session(token: Optional[str]) -> Optional[IframeSession]:
    if not token:
        return None
    return _sessions.get(token)


def revoke_iframe_session(token: str) -> bool:
    return _sessions.revoke(token)


def mint_standalone_session(*, subdomain: str = "standalone",
                            user_email: str = "operator@localhost",
                            user_id: int = 0) -> IframeSession:
    """Mint a session WITHOUT HMAC verification. Only callable when the
    backend is running in standalone mode — the route that exposes this
    enforces that flag. Used when the operator browses the UI directly
    instead of through the Zendesk iframe."""
    sess = IframeSession(
        token=_new_token(),
        subdomain=subdomain,
        user_id=user_id,
        user_email=user_email,
    )
    _sessions.put(sess)
    return sess
