"""
server.app — FastAPI HTTP routes.

All endpoints live under `/api/v1`. The iframe is expected to carry an
`Authorization: Bearer <iframe-session-token>` header (issued by
/session). The OAuth callback is the single exception — it's reached
by Zendesk's browser redirect and uses the `state` parameter for
verification.

Error envelope is consistent:
    { "error": "<message>", "kind": "<class>" }
with HTTP 400 for client errors, 401/403 for auth, 404 for unknown
resources, 500 for server-side bugs.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pathlib import Path as FsPath

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse


from server.events import (
    get_log_tail,
    get_status,
    request_cancel,
    stream_events,
)
from server.jobs import (
    run_cleanup,
    run_format_target,
    run_full_migration,
    run_preflight,
    run_restore,
    run_rollback,
)
from server.oauth import (
    begin_oauth,
    complete_oauth,
    establish_iframe_session,
    get_iframe_session,
    mint_standalone_session,
    parse_redirect_url,
    refresh_oauth_connection,
    revoke_iframe_session,
    sign_hmac,
)
from server.settings import get_settings
from server.state import (
    Connection,
    get_connection_store,
    is_valid_migration_id,
    new_migration_id,
    report_path_for,
    state_dir_for,
)


app = FastAPI(title="zd-transfer backend", version="0.1.0")


# ------------------------------------------------------------------ #
#  CORS                                                               #
# ------------------------------------------------------------------ #

_settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Signature"],
    max_age=86400,
)


# ------------------------------------------------------------------ #
#  Auth dependency                                                    #
# ------------------------------------------------------------------ #

def require_session(
    authorization: Optional[str] = Header(None),
    t: Optional[str] = Query(None),
) -> "object":
    """Validate the iframe session token. Returns the IframeSession
    object on success, raises 401 otherwise.

    Token sources, in priority order:
      1. `Authorization: Bearer <token>` header (preferred — used by
         every fetch() call).
      2. `?t=<token>` query parameter (fallback for browser flows
         that can't set headers: <a href> downloads, EventSource SSE).

    Routes that don't need iframe auth (health, oauth/*, session)
    omit this dependency.
    """
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
    elif t:
        token = t.strip()
    if not token:
        raise HTTPException(status_code=401, detail={"error": "missing or malformed Authorization header", "kind": "AuthError"})
    sess = get_iframe_session(token)
    if sess is None:
        raise HTTPException(status_code=401, detail={"error": "session is invalid or expired", "kind": "AuthError"})
    return sess


# ------------------------------------------------------------------ #
#  Pydantic schemas                                                   #
# ------------------------------------------------------------------ #

class OAuthStartRequest(BaseModel):
    role: str = Field(..., pattern="^(source|target)$")
    subdomain: str
    client_id: str
    client_secret: str
    scope: Optional[str] = None


class PreflightRequest(BaseModel):
    source_connection_id: str
    target_connection_id: str


class MigrateRequest(BaseModel):
    source_connection_id: str
    target_connection_id: str
    phases: Optional[List[int]] = None
    max_users: Optional[int] = None
    users_from: int = 0
    dry_run: bool = False
    format_target: bool = False


class DirectConnectRequest(BaseModel):
    role: str = Field(..., pattern="^(source|target)$")
    subdomain: str
    api_token: str


class OAuthExchangeRedirectRequest(BaseModel):
    redirect_url: str


class FormatRequest(BaseModel):
    target_connection_id: str
    dry_run: bool = False


class CleanupRequest(BaseModel):
    target_connection_id: str


class RollbackRequest(BaseModel):
    target_connection_id: str
    phase: int = Field(..., ge=1, le=3)


class RestoreRequest(BaseModel):
    target_connection_id: str
    backup_path: str


# ------------------------------------------------------------------ #
#  RQ enqueue helper                                                  #
# ------------------------------------------------------------------ #

def _enqueue(job_callable, *args, **kwargs) -> str:
    """Enqueue a job onto RQ. Returns the RQ job id. We deliberately
    don't expose the RQ id to the iframe — the iframe tracks the
    migration_id, which is independent.

    Falls back to synchronous execution when ZDX_DEV_MODE is set so
    `uvicorn` alone (without a separate `rq worker` process) still
    runs end-to-end. Production deployments MUST run a worker.
    """
    if get_settings().dev_mode:
        # Inline execution: blocks the HTTP request for the job's
        # duration. Useful for local dev / smoke tests. Errors don't
        # propagate — same contract as the worker.
        try:
            job_callable(*args, **kwargs)
        except Exception:
            import traceback
            traceback.print_exc()
        return "inline"

    import redis
    from rq import Queue
    s = get_settings()
    conn = redis.Redis.from_url(s.redis_url)
    q = Queue(s.rq_queue_name, connection=conn, default_timeout=s.rq_job_timeout)
    job = q.enqueue(job_callable, *args, **kwargs)
    return job.id


# ------------------------------------------------------------------ #
#  Routes                                                             #
# ------------------------------------------------------------------ #

@app.get("/api/v1/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "version": app.version}


@app.get("/api/v1/config")
def public_config() -> Dict[str, Any]:
    """Public introspection — tells the UI whether it should run the
    Zendesk-iframe boot sequence or the standalone fallback. No secrets
    here; safe to call unauthenticated."""
    s = get_settings()
    return {
        "standalone_mode": s.standalone_mode,
        "version": app.version,
    }


# ---- session ------------------------------------------------------ #

@app.post("/api/v1/session")
async def post_session(
    request: Request,
    x_signature: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Establish an iframe session. Body is the ZAFClient.context()
    envelope; X-Signature is HMAC-SHA256(body, ZDX_HMAC_SECRET).
    """
    body = await request.body()
    try:
        sess = establish_iframe_session(
            body_bytes=body,
            signature_hex=(x_signature or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail={"error": str(exc), "kind": "AuthError"})
    return {
        "token": sess.token,
        "subdomain": sess.subdomain,
        "user_id": sess.user_id,
        "user_email": sess.user_email,
    }


@app.delete("/api/v1/session")
def delete_session(sess=Depends(require_session)) -> Dict[str, bool]:
    revoke_iframe_session(sess.token)
    return {"revoked": True}


@app.post("/api/v1/standalone/session")
def post_standalone_session() -> Dict[str, Any]:
    """No-HMAC session for the bundled standalone UI. Only available
    when ZDX_STANDALONE_MODE=1 — otherwise 404, so a misconfigured
    production deployment doesn't accidentally expose an auth bypass."""
    if not get_settings().standalone_mode:
        raise HTTPException(status_code=404,
                            detail={"error": "standalone mode disabled",
                                    "kind": "NotFound"})
    sess = mint_standalone_session()
    return {
        "token": sess.token,
        "subdomain": sess.subdomain,
        "user_id": sess.user_id,
        "user_email": sess.user_email,
    }


# ---- OAuth -------------------------------------------------------- #

@app.post("/api/v1/oauth/start")
def oauth_start(
    body: OAuthStartRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    try:
        url, state = begin_oauth(
            role=body.role,
            subdomain=body.subdomain,
            client_id=body.client_id,
            client_secret=body.client_secret,
            scope=body.scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "kind": "ValueError"})
    return {"authorize_url": url, "state": state}


@app.get("/api/v1/oauth/callback")
def oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
) -> Response:
    """Zendesk's redirect target. Renders a small HTML page that
    `postMessage()`s the result back to the iframe (the popup's
    opener) and self-closes.
    """
    if error:
        return _callback_page(ok=False, message=f"{error}: {error_description or ''}")
    if not code or not state:
        return _callback_page(ok=False, message="missing code or state")
    try:
        conn = complete_oauth(state=state, code=code)
    except ValueError as exc:
        return _callback_page(ok=False, message=str(exc))
    return _callback_page(
        ok=True,
        message="Connected.",
        payload={
            "connection_id": conn.id,
            "role": conn.role,
            "subdomain": conn.subdomain,
        },
    )


def _callback_page(*, ok: bool, message: str,
                   payload: Optional[Dict] = None) -> HTMLResponse:
    """Render a tiny self-closing HTML page for the OAuth popup.

    NOTE: targetOrigin is intentionally "*" because the popup's
    opener (the ZAF iframe) is served from a Zendesk-controlled host
    whose exact URL we cannot reliably predict from inside the
    callback. The message body itself contains nothing sensitive — at
    most the (already-public) subdomain string and a connection id.
    The actual OAuth token is held by the backend; the iframe only
    sees the masked connection metadata.
    """
    safe_message = html.escape(message)
    body_json = json.dumps({"ok": ok, "message": message, "payload": payload or {}})
    html_body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Connecting…</title></head>
<body>
<p>{'Connected — you can close this window.' if ok else safe_message}</p>
<script>
  (function() {{
    var msg = {body_json};
    try {{
      if (window.opener && !window.opener.closed) {{
        window.opener.postMessage({{ type: 'zdx-oauth-callback', detail: msg }}, '*');
      }}
    }} catch (e) {{ /* opener might be cross-origin; iframe must poll fallback */ }}
    window.setTimeout(function() {{ window.close(); }}, 800);
  }})();
</script>
</body>
</html>"""
    return HTMLResponse(content=html_body, status_code=200 if ok else 400)


@app.post("/api/v1/oauth/exchange-redirect")
def oauth_exchange_redirect(
    body: OAuthExchangeRedirectRequest,
    sess=Depends(require_session),
) -> Dict[str, Any]:
    """CLI-parity OAuth exchange: operator pastes the redirect URL they
    copied from the browser after authorizing at Zendesk. The backend
    extracts `code` and `state` from it, exchanges the code for a bearer
    token, and persists the connection — same as the popup callback but
    without the HTML rigmarole."""
    try:
        code, state = parse_redirect_url(body.redirect_url)
        conn = complete_oauth(state=state, code=code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "kind": "ValueError"})
    return {
        "connection_id": conn.id,
        "role": conn.role,
        "subdomain": conn.subdomain,
    }


# ---- Connections -------------------------------------------------- #

@app.post("/api/v1/connections")
def create_connection(
    body: DirectConnectRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    """Create a connection directly from an API token — no OAuth dance.
    Mirrors the CLI's ability to skip OAuth when the operator already
    has a token (e.g. from a .env file)."""
    conn = Connection(
        id=new_migration_id(),
        role=body.role,
        subdomain=body.subdomain.strip().lower() if body.subdomain else "",
        auth_kind="api_token",
        api_token=body.api_token,
    )
    get_connection_store().put(conn)
    return {
        "connection_id": conn.id,
        "role": conn.role,
        "subdomain": conn.subdomain,
    }


@app.get("/api/v1/connections")
def list_connections(
    role: Optional[str] = Query(None, pattern="^(source|target)$"),
    sess=Depends(require_session),
) -> Dict[str, List[Dict]]:
    store = get_connection_store()
    return {"connections": [c.masked() for c in store.list(role=role)]}


@app.delete("/api/v1/connections/{conn_id}")
def delete_connection(
    conn_id: str = PathParam(...),
    sess=Depends(require_session),
) -> Dict[str, bool]:
    if not get_connection_store().delete(conn_id):
        raise HTTPException(status_code=404, detail={"error": f"unknown connection: {conn_id}", "kind": "NotFound"})
    return {"deleted": True}


@app.post("/api/v1/connections/{conn_id}/refresh")
def refresh_connection(
    conn_id: str = PathParam(...),
    sess=Depends(require_session),
) -> Dict[str, object]:
    """Mint a new OAuth access token using the stored refresh_token.

    Returns the masked connection record so the UI can display the
    updated last-4 of the token without forcing a list refresh.
    Errors are mapped to 4xx with operator-readable messages.
    """
    try:
        conn = refresh_oauth_connection(conn_id)
    except ValueError as exc:
        msg = str(exc)
        # 404 only for "unknown connection"; the other guard conditions
        # (api_token kind, missing client creds, Zendesk reject) are
        # operator-actionable but the record itself exists → 400.
        status = 404 if msg.startswith("unknown connection") else 400
        raise HTTPException(
            status_code=status,
            detail={"error": msg, "kind": "RefreshFailed"},
        )
    return {"refreshed": True, "connection": conn.masked()}


# ---- Preflight + Jobs --------------------------------------------- #

@app.post("/api/v1/preflight")
def preflight(
    body: PreflightRequest,
    sess=Depends(require_session),
) -> Dict[str, Any]:
    """Synchronous (fast) — pings both accounts and returns the
    baseline scan. No state directory is touched."""
    return run_preflight(body.source_connection_id, body.target_connection_id)


@app.post("/api/v1/jobs/migrate")
def jobs_migrate(
    body: MigrateRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    mid = new_migration_id()
    rq_id = _enqueue(
        run_full_migration,
        mid,
        body.source_connection_id,
        body.target_connection_id,
        phases=body.phases,
        max_users=body.max_users,
        users_from=body.users_from,
        dry_run=body.dry_run,
        format_target=body.format_target,
    )
    return {"migration_id": mid, "rq_job_id": rq_id}


@app.post("/api/v1/jobs/format")
def jobs_format(
    body: FormatRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    mid = new_migration_id()
    rq_id = _enqueue(
        run_format_target,
        mid,
        body.target_connection_id,
        dry_run=body.dry_run,
    )
    return {"migration_id": mid, "rq_job_id": rq_id}


@app.post("/api/v1/jobs/cleanup")
def jobs_cleanup(
    body: CleanupRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    mid = new_migration_id()
    rq_id = _enqueue(run_cleanup, mid, body.target_connection_id)
    return {"migration_id": mid, "rq_job_id": rq_id}


@app.post("/api/v1/jobs/rollback")
def jobs_rollback(
    body: RollbackRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    mid = new_migration_id()
    rq_id = _enqueue(run_rollback, mid, body.target_connection_id, body.phase)
    return {"migration_id": mid, "rq_job_id": rq_id}


@app.post("/api/v1/jobs/restore")
def jobs_restore(
    body: RestoreRequest,
    sess=Depends(require_session),
) -> Dict[str, str]:
    mid = new_migration_id()
    rq_id = _enqueue(run_restore, mid, body.target_connection_id, body.backup_path)
    return {"migration_id": mid, "rq_job_id": rq_id}


@app.get("/api/v1/backups")
def list_backups(
    sess=Depends(require_session),
) -> Dict[str, Any]:
    from src.backup import list_backups as _list_backups
    items = []
    for d in _list_backups():
        meta_path = d / "metadata.json"
        meta = {}
        if meta_path.exists():
            try:
                import json
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        items.append({
            "path": str(d),
            "name": d.name,
            "resource_count": meta.get("resource_count"),
            "timestamp": meta.get("timestamp", d.name),
        })
    return {"backups": items}


@app.get("/api/v1/jobs/{migration_id}/status")
def jobs_status(
    migration_id: str = PathParam(...),
    tail: int = Query(20, ge=0, le=500),
    sess=Depends(require_session),
) -> Dict[str, Any]:
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})
    return {
        "migration_id": migration_id,
        "status": get_status(migration_id),
        "log_tail": get_log_tail(migration_id, n=tail),
    }


@app.get("/api/v1/jobs/{migration_id}/events")
async def jobs_events(
    migration_id: str = PathParam(...),
    authorization: Optional[str] = Header(None),
    t: Optional[str] = Query(None),
):
    """Server-Sent Events stream. SSE doesn't allow custom headers in
    EventSource, so the iframe passes the session token as a query
    parameter `?t=...` OR via Authorization header (we accept both).
    """
    require_session(authorization, t=t)
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})

    return EventSourceResponse(stream_events(migration_id))


@app.post("/api/v1/jobs/{migration_id}/cancel")
def jobs_cancel(
    migration_id: str = PathParam(...),
    sess=Depends(require_session),
) -> Dict[str, bool]:
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})
    request_cancel(migration_id)
    return {"cancel_requested": True}


@app.get("/api/v1/migrations")
def list_migrations(
    sess=Depends(require_session),
) -> Dict[str, List[Dict[str, Any]]]:
    """List past migration runs (last 3 days) with metadata about
    available reports, logs, and id-maps."""
    root = get_settings().state_root
    if not root.is_dir():
        return {"migrations": []}

    cutoff = datetime.now(timezone.utc).timestamp() - (3 * 86400)
    items: List[Dict[str, Any]] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir() or not is_valid_migration_id(entry.name):
            continue
        mtime = entry.stat().st_mtime
        if mtime < cutoff:
            continue
        mid = entry.name
        status = get_status(mid) or {}
        items.append({
            "migration_id": mid,
            "created_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "phase": status.get("phase") or "unknown",
            "has_report": (entry / "migration_report.md").is_file(),
            "has_log": (entry / "migration_log.jsonl").is_file(),
            "has_id_map": (entry / "id_map.json").is_file(),
        })

    return {"migrations": items}


@app.get("/api/v1/migrations/{migration_id}/report")
def migrations_report(
    migration_id: str = PathParam(...),
    download: int = Query(0, ge=0, le=1),
    sess=Depends(require_session),
) -> Response:
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})
    p = report_path_for(migration_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail={"error": "report not found yet", "kind": "NotFound"})
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="migration_report_{migration_id}.md"'
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers=headers,
    )


@app.get("/api/v1/migrations/{migration_id}/log")
def migrations_log(
    migration_id: str = PathParam(...),
    download: int = Query(0, ge=0, le=1),
    sess=Depends(require_session),
) -> Response:
    """Return the structured JSONL audit log for a migration run."""
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})
    from server.state import log_path_for
    p = log_path_for(migration_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail={"error": "log not found", "kind": "NotFound"})
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="migration_log_{migration_id}.jsonl"'
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/x-ndjson; charset=utf-8",
        headers=headers,
    )


@app.get("/api/v1/migrations/{migration_id}/id-map")
def migrations_id_map(
    migration_id: str = PathParam(...),
    download: int = Query(0, ge=0, le=1),
    sess=Depends(require_session),
) -> Response:
    """Return the source→target id_map.json for a migration run."""
    if not is_valid_migration_id(migration_id):
        raise HTTPException(status_code=400, detail={"error": "invalid migration_id", "kind": "ValueError"})
    from server.state import id_map_path_for
    p = id_map_path_for(migration_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail={"error": "id_map not found", "kind": "NotFound"})
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="id_map_{migration_id}.json"'
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


# ------------------------------------------------------------------ #
#  Static UI                                                          #
# ------------------------------------------------------------------ #

def _mount_ui() -> None:
    """Mount the built React bundle at `/`. Runs once at import time.
    Disabled if ZDX_UI_DIR is unset OR the directory is empty.

    The Vite build emits `iframe.html` as its document plus hashed JS
    chunks. We serve the directory at `/static/...` and intercept `/`
    and `/iframe.html` to return `iframe.html`."""
    ui_dir = get_settings().ui_dir
    if not ui_dir:
        return
    p = FsPath(ui_dir)
    if not p.is_dir():
        import sys
        print(f"[ui] ZDX_UI_DIR set to {ui_dir!r} but directory not found; "
              "UI serving disabled", file=sys.stderr)
        return

    entry = p / "iframe.html"
    if not entry.is_file():
        import sys
        print(f"[ui] {entry} missing — UI bundle not built? "
              "Run `./scripts/build_app.sh`.", file=sys.stderr)
        return

    # Hashed JS/CSS assets live in the same directory as iframe.html
    # (Vite's default). Mount the whole directory under /static; any
    # 404 there is genuinely missing, not a SPA route.
    app.mount("/static", StaticFiles(directory=str(p)), name="ui-static")

    @app.api_route("/", methods=["GET", "HEAD"])
    @app.api_route("/iframe.html", methods=["GET", "HEAD"])
    def _ui_root() -> FileResponse:
        return FileResponse(str(entry), media_type="text/html")

    # Vite's emitted iframe.html references its JS chunk by a relative
    # path like `./app-XXX.js`. When that page is served at `/`, the
    # browser resolves `./app-XXX.js` to `/app-XXX.js`. Add a catch-all
    # for top-level asset filenames so those requests hit the same
    # files we'd serve under /static.
    @app.api_route("/{filename:path}", methods=["GET", "HEAD"])
    def _ui_asset(filename: str) -> Response:
        # API routes are registered earlier; FastAPI matches them first.
        # This handler is only reached for non-API paths.
        target = p / filename
        # Refuse path-traversal attempts.
        try:
            target.resolve().relative_to(p.resolve())
        except ValueError:
            raise HTTPException(status_code=404)
        if target.is_file():
            return FileResponse(str(target))
        raise HTTPException(status_code=404)


_mount_ui()


# ------------------------------------------------------------------ #
#  Global exception → JSON                                            #
# ------------------------------------------------------------------ #

@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so the iframe always sees JSON, never an
    HTML error page that confuses fetch()."""
    import sys, traceback
    print(f"[unhandled] {type(exc).__name__}: {exc}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}", "kind": "InternalError"},
    )
