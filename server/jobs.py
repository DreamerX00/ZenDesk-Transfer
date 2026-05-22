"""
server.jobs — RQ job definitions that wrap the existing phase code.

Each job is a top-level function (RQ requires picklable callables).
The wrapper does three things in order:

  1. Bind `current_migration_id` and `event_sink` ContextVars so the
     existing logger / importer route their state to the right place.
  2. Build ZendeskClient instances from the encrypted ConnectionStore.
  3. Call the existing `phase*.run()` functions unchanged.

If the migration is cancelled mid-flight (the iframe POSTed
/jobs/{id}/cancel), the cancellation flag is checked at phase
boundaries — we don't yank a phase mid-write because partial writes
to a paginated bulk endpoint would leave the target in an
inconsistent state. The current phase finishes; later phases skip.

The CLI is never imported here. Everything we need lives in
src/{client,extractor,formatter,phases}.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.client import ZendeskClient
from src.extractor import extract_all
from src.formatter import execute as fmt_execute
from src.utils import logger
from src.utils.runctx import current_migration_id, event_sink

from server.events import (
    is_cancelled,
    make_event_sink,
    set_status,
)
from server.state import Connection, get_connection_store


# ------------------------------------------------------------------ #
#  Client construction                                                #
# ------------------------------------------------------------------ #

def _build_client(conn: Connection, *, dry_run: bool = False) -> ZendeskClient:
    """Construct a ZendeskClient from a stored Connection.

    NOTE: ZendeskClient's __init__ accepts `env_path` as the file it
    writes a refreshed OAuth token back into. The server-side flow has
    no such file — token refreshes need to round-trip through the
    ConnectionStore. For now we pass env_path=None; the client will
    skip the refresh-token persistence step (it already tolerates a
    missing env_path).
    """
    if conn.auth_kind == "oauth":
        return ZendeskClient(
            subdomain=conn.subdomain,
            oauth_token=conn.oauth_token,
            oauth_refresh_token=conn.oauth_refresh_token,
            oauth_client_id=conn.oauth_client_id,
            oauth_client_secret=conn.oauth_client_secret,
            env_path=None,
            dry_run=dry_run,
        )
    if conn.auth_kind == "api_token":
        return ZendeskClient(
            subdomain=conn.subdomain,
            email=conn.email,
            api_token=conn.api_token,
            dry_run=dry_run,
        )
    raise ValueError(f"unsupported auth_kind: {conn.auth_kind!r}")


# ------------------------------------------------------------------ #
#  Shared phase orchestration                                         #
# ------------------------------------------------------------------ #

def _bind_run_context(migration_id: str):
    """Set the per-migration ContextVars. Returns a token-bundle that
    can be reset on exit. Pattern is `with ContextVarsScope(...)` but
    we expose plain tokens because RQ jobs are not async."""
    mid_tok = current_migration_id.set(migration_id)
    sink_tok = event_sink.set(make_event_sink(migration_id))
    return (mid_tok, sink_tok)


def _release_run_context(tokens) -> None:
    mid_tok, sink_tok = tokens
    try:
        event_sink.reset(sink_tok)
    except ValueError:
        pass
    try:
        current_migration_id.reset(mid_tok)
    except ValueError:
        pass


def _status_update(migration_id: str, **fields: Any) -> None:
    set_status(migration_id, **fields)


def _emit_phase_start(migration_id: str, phase_name: str) -> None:
    _status_update(
        migration_id,
        phase=phase_name,
        phase_started_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.section(f"Phase: {phase_name}")


# ------------------------------------------------------------------ #
#  Public jobs                                                        #
# ------------------------------------------------------------------ #

def run_full_migration(
    migration_id: str,
    source_conn_id: str,
    target_conn_id: str,
    *,
    phases: Optional[List[int]] = None,
    max_users: Optional[int] = None,
    users_from: int = 0,
    dry_run: bool = False,
    format_target: bool = False,
) -> Dict[str, Any]:
    """Top-level migration job. Equivalent to `cmd_run` / `cmd_migrate`
    in main.py but works headlessly with in-memory credentials and
    streams events.

    Args:
      migration_id: ContextVar key; also where state lands on disk.
      source_conn_id / target_conn_id: ConnectionStore ids.
      phases: list of phase numbers to run (1, 2, 3, 4, 5). None = all.
      max_users / users_from: phase 5 chunking knobs.
      dry_run: if True, target client runs in dry-run mode.
      format_target: if True, run formatter.execute on the target
        before any phase. Idempotent on an already-empty target.

    Returns a small summary dict the API persists on the job.
    """
    tokens = _bind_run_context(migration_id)
    summary: Dict[str, Any] = {
        "migration_id": migration_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "phases_run": [],
    }
    _status_update(
        migration_id,
        phase="starting",
        dry_run=str(dry_run),
        format_target=str(format_target),
        max_users=("" if max_users is None else max_users),
        users_from=users_from,
    )

    try:
        store = get_connection_store()
        source_conn = store.get(source_conn_id)
        target_conn = store.get(target_conn_id)
        if source_conn is None:
            raise ValueError(f"unknown source connection: {source_conn_id}")
        if target_conn is None:
            raise ValueError(f"unknown target connection: {target_conn_id}")
        if source_conn.role != "source":
            raise ValueError(f"connection {source_conn_id} is not a source")
        if target_conn.role != "target":
            raise ValueError(f"connection {target_conn_id} is not a target")

        source = _build_client(source_conn, dry_run=False)
        target = _build_client(target_conn, dry_run=dry_run)

        # Rate-limit calibration mirrors _load_client in main.py.
        try:
            source_rpm = source.prefetch_plan()
            target_rpm = target.prefetch_plan()
            _status_update(migration_id, source_rpm=source_rpm, target_rpm=target_rpm)
        except Exception:
            pass

        # ---- Optional format-target ---------------------------------- #
        if format_target:
            if is_cancelled(migration_id):
                _status_update(migration_id, phase="cancelled")
                summary["cancelled"] = True
                return summary
            _emit_phase_start(migration_id, "format-target")
            fmt_execute(target)
            summary["phases_run"].append("format-target")

        # ---- Extract --------------------------------------------------- #
        if is_cancelled(migration_id):
            _status_update(migration_id, phase="cancelled")
            summary["cancelled"] = True
            return summary

        _emit_phase_start(migration_id, "extract")
        exports = extract_all(source)
        summary["phases_run"].append("extract")
        # Publish extracted resource counts for frontend estimation.
        for rkey, items in exports.items():
            if isinstance(items, list):
                _status_update(migration_id, **{f"extracted_{rkey}": str(len(items))})

        # ---- Phases ---------------------------------------------------- #
        from src.phases import (
            phase1_foundation, phase2_business_logic,
            phase3_content, phase4_verify, phase5_users,
        )

        want = set(phases) if phases is not None else {1, 2, 3, 4, 5}
        # Phase 3 runs after phase 1 but before phase 2 (mirroring cmd_run).
        ordered: List[tuple] = [
            (1, "1-foundation",       lambda: phase1_foundation.run(source, target, exports)),
            (3, "3-content",          lambda: phase3_content.run(source, target, exports)),
            (2, "2-business-logic",   lambda: phase2_business_logic.run(source, target, exports)),
            (5, "5-users",            lambda: phase5_users.run(
                source, target, exports,
                max_users=max_users,
                users_from=users_from,
                assume_yes=True,  # UI-driven: a textual prompt would hang
            )),
            (4, "4-verify",           lambda: phase4_verify.run(source, target)),
        ]

        for num, name, fn in ordered:
            if num not in want:
                continue
            if is_cancelled(migration_id):
                _status_update(migration_id, phase="cancelled")
                summary["cancelled"] = True
                return summary
            _emit_phase_start(migration_id, name)
            fn()
            summary["phases_run"].append(name)

        _status_update(
            migration_id,
            phase="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["status"] = "completed"
        return summary

    except Exception as exc:
        _status_update(
            migration_id,
            phase="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        # Make the failure visible in the event stream too.
        try:
            logger.error(f"Job failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        return summary
    finally:
        _release_run_context(tokens)


def run_format_target(migration_id: str, target_conn_id: str, *,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Standalone format-target job (no migration, no source)."""
    tokens = _bind_run_context(migration_id)
    summary: Dict[str, Any] = {
        "migration_id": migration_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
    }
    _status_update(migration_id, phase="starting", action="format-target")
    try:
        store = get_connection_store()
        target_conn = store.get(target_conn_id)
        if target_conn is None:
            raise ValueError(f"unknown target connection: {target_conn_id}")
        target = _build_client(target_conn, dry_run=dry_run)
        try:
            target.prefetch_plan()
        except Exception:
            pass
        _emit_phase_start(migration_id, "format-target")
        fmt_execute(target)
        _status_update(migration_id, phase="completed",
                       finished_at=datetime.now(timezone.utc).isoformat())
        summary["status"] = "completed"
    except Exception as exc:
        _status_update(migration_id, phase="failed",
                       error=f"{type(exc).__name__}: {exc}")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
    finally:
        _release_run_context(tokens)
    return summary


def run_preflight(
    source_conn_id: str,
    target_conn_id: str,
) -> Dict[str, Any]:
    """Synchronous-style preflight (still callable from RQ if needed
    for retry semantics). Does NOT bind a migration_id ContextVar —
    preflight is read-only and not associated with any future state
    directory.
    """
    from src.formatter import preview as fmt_preview

    out: Dict[str, Any] = {
        "source": {"ok": False},
        "target": {"ok": False},
        "source_baseline": [],
        "baseline": [],
    }

    store = get_connection_store()
    source_conn = store.get(source_conn_id)
    target_conn = store.get(target_conn_id)
    if source_conn is None:
        out["source"]["error"] = f"unknown connection: {source_conn_id}"
        return out
    if target_conn is None:
        out["target"]["error"] = f"unknown connection: {target_conn_id}"
        return out

    try:
        source = _build_client(source_conn)
        info = source.ping() or {}
        acct = info.get("account", info) if isinstance(info, dict) else {}
        out["source"] = {
            "ok": True,
            "subdomain": source_conn.subdomain,
            "account_name": acct.get("name") if isinstance(acct, dict) else None,
        }
        try:
            out["source_baseline"] = fmt_preview(source)
        except Exception as exc:
            out["source_baseline_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        out["source"] = {
            "ok": False,
            "subdomain": source_conn.subdomain,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        target = _build_client(target_conn)
        info = target.ping() or {}
        acct = info.get("account", info) if isinstance(info, dict) else {}
        out["target"] = {
            "ok": True,
            "subdomain": target_conn.subdomain,
            "account_name": acct.get("name") if isinstance(acct, dict) else None,
        }
        try:
            out["baseline"] = fmt_preview(target)
        except Exception as exc:
            out["baseline_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        out["target"] = {
            "ok": False,
            "subdomain": target_conn.subdomain,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return out


# ------------------------------------------------------------------ #
#  Cleanup / Rollback / Restore jobs                                  #
# ------------------------------------------------------------------ #

_ALL_RESOURCES_ORDERED = [
    ("hc_articles",           "help_center/articles/{id}"),
    ("hc_sections",           "help_center/sections/{id}"),
    ("hc_categories",         "help_center/categories/{id}"),
    ("hc_user_segments",      "help_center/user_segments/{id}"),
    ("webhooks",              "webhooks/{id}"),
    ("dynamic_content_items", "dynamic_content/items/{id}"),
    ("routing_attributes",    "routing/attributes/{id}"),
    ("schedules",             "business_hours/schedules/{id}"),
    ("group_sla_policies",    "group_slas/policies/{id}"),
    ("sla_policies",          "slas/policies/{id}"),
    ("macros",                "macros/{id}"),
    ("automations",           "automations/{id}"),
    ("triggers",              "triggers/{id}"),
    ("views",                 "views/{id}"),
    ("organizations",         "organizations/{id}"),
    ("ticket_forms",          "ticket_forms/{id}"),
    ("custom_roles",          "custom_roles/{id}"),
    ("organization_fields",   "organization_fields/{id}"),
    ("user_fields",           "user_fields/{id}"),
    ("ticket_fields",         "ticket_fields/{id}"),
    ("brands",                "brands/{id}"),
    ("groups",                "groups/{id}"),
]

_PHASE_RESOURCE_KEYS = {
    1: [
        "organizations", "ticket_forms", "custom_roles",
        "organization_fields", "user_fields", "ticket_fields",
        "brands", "groups",
    ],
    2: [
        "webhooks", "dynamic_content_items", "routing_attributes",
        "schedules", "group_sla_policies", "sla_policies",
        "macros", "automations", "triggers", "views",
    ],
    3: [
        "hc_articles", "hc_sections", "hc_categories", "hc_user_segments",
    ],
}


def _delete_idmap_resources(
    target: ZendeskClient,
    id_map: dict,
    resource_keys: list,
    label: str = "",
) -> int:
    delete_path_map = dict(_ALL_RESOURCES_ORDERED)
    deleted = 0
    for rkey in resource_keys:
        mappings = id_map.get(rkey)
        if not isinstance(mappings, dict) or not mappings:
            continue
        tpl = delete_path_map.get(rkey, "")
        if not tpl or "{id}" not in tpl:
            continue
        for source_id, target_id in list(mappings.items()):
            if not target_id or str(target_id).strip() in ("", "None"):
                continue
            path = tpl.replace("{id}", str(target_id))
            try:
                target.delete(path)
                deleted += 1
            except ZendeskAPIError as exc:
                if exc.status_code == 404:
                    deleted += 1
            except ZendeskNetworkError:
                pass
    return deleted


def _reset_state_files(state_dir) -> None:
    import json, tempfile, os
    id_map_path = state_dir / "id_map.json"
    log_path = state_dir / "migration_log.jsonl"
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({}, f)
        os.replace(tmp, id_map_path)
    except Exception:
        pass
    try:
        log_path.write_text("", encoding="utf-8")
    except Exception:
        pass


def run_cleanup(
    migration_id: str,
    target_conn_id: str,
) -> Dict[str, Any]:
    """Full rollback — delete every resource in the id_map."""
    tokens = _bind_run_context(migration_id)
    summary: Dict[str, Any] = {
        "migration_id": migration_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _status_update(migration_id, phase="starting", action="cleanup")
    try:
        store = get_connection_store()
        target_conn = store.get(target_conn_id)
        if target_conn is None:
            raise ValueError(f"unknown target connection: {target_conn_id}")
        target = _build_client(target_conn, dry_run=False)
        try:
            target.prefetch_plan()
        except Exception:
            pass

        from src.importer import load_id_map, STATE_DIR
        id_map = load_id_map()
        all_keys = [rkey for rkey, _ in _ALL_RESOURCES_ORDERED]
        deleted = _delete_idmap_resources(target, id_map, all_keys, label="cleanup")
        _reset_state_files(STATE_DIR)

        _status_update(
            migration_id, phase="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            deleted=str(deleted),
        )
        summary["status"] = "completed"
        summary["deleted"] = deleted
    except Exception as exc:
        _status_update(migration_id, phase="failed",
                       error=f"{type(exc).__name__}: {exc}")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
    finally:
        _release_run_context(tokens)
    return summary


def run_rollback(
    migration_id: str,
    target_conn_id: str,
    phase: int,
) -> Dict[str, Any]:
    """Roll back one specific phase."""
    tokens = _bind_run_context(migration_id)
    summary: Dict[str, Any] = {
        "migration_id": migration_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _status_update(migration_id, phase="starting", action=f"rollback-phase-{phase}")
    try:
        store = get_connection_store()
        target_conn = store.get(target_conn_id)
        if target_conn is None:
            raise ValueError(f"unknown target connection: {target_conn_id}")
        target = _build_client(target_conn, dry_run=False)
        try:
            target.prefetch_plan()
        except Exception:
            pass

        resource_keys = _PHASE_RESOURCE_KEYS.get(phase)
        if not resource_keys:
            raise ValueError(f"no rollback definition for phase {phase}")

        from src.importer import load_id_map
        id_map = load_id_map()
        deleted = _delete_idmap_resources(target, id_map, resource_keys,
                                          label=f"phase{phase}")

        _status_update(
            migration_id, phase="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            deleted=str(deleted),
        )
        summary["status"] = "completed"
        summary["deleted"] = deleted
    except Exception as exc:
        _status_update(migration_id, phase="failed",
                       error=f"{type(exc).__name__}: {exc}")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
    finally:
        _release_run_context(tokens)
    return summary


def run_restore(
    migration_id: str,
    target_conn_id: str,
    backup_path: str,
) -> Dict[str, Any]:
    """Restore target from a backup directory."""
    tokens = _bind_run_context(migration_id)
    summary: Dict[str, Any] = {
        "migration_id": migration_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _status_update(migration_id, phase="starting", action="restore")
    try:
        store = get_connection_store()
        target_conn = store.get(target_conn_id)
        if target_conn is None:
            raise ValueError(f"unknown target connection: {target_conn_id}")
        target = _build_client(target_conn, dry_run=False)
        try:
            target.prefetch_plan()
        except Exception:
            pass

        from pathlib import Path
        from src.backup import restore as bk_restore
        backup_dir = Path(backup_path)
        if not backup_dir.is_dir():
            raise ValueError(f"backup directory not found: {backup_path}")
        created = bk_restore(target, backup_dir)

        _status_update(
            migration_id, phase="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            created=str(created),
        )
        summary["status"] = "completed"
        summary["created"] = created
    except Exception as exc:
        _status_update(migration_id, phase="failed",
                       error=f"{type(exc).__name__}: {exc}")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
    finally:
        _release_run_context(tokens)
    return summary
