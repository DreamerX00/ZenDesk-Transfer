"""
Importer — creates resources in the TARGET account.

Conflict policy: if a resource with the same name/key already exists
in the target, it is DELETED first (PURGE), then the source version
is created fresh. This guarantees an exact mirror of the source.

Bug fixes vs v1:
  - load_id_map() now handles corrupted/malformed JSON gracefully.
  - save_id_map() uses atomic write (temp file + rename) to prevent
    partial writes corrupting state on crash.
  - target_id=None after create is now caught and reported as failure
    rather than silently storing "None" → "None" in the map.
  - Empty-string name_field collisions (nameless items) no longer
    cause spurious purges of each other.
  - Pre-process fn exceptions are caught and logged.
"""

import atexit
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.remapper import remap_payload, strip_source_fields, RemapError
from src.utils import logger
from src.utils.runctx import current_migration_id, state_dir as _ctx_state_dir

# Legacy module-level paths. Kept for backwards compatibility with
# callers in main.py that import them directly (`from src.importer
# import STATE_DIR`). These always resolve to the repo-level `state/`
# dir — the CLI's single-run case. Concurrent / per-migration callers
# must NOT use these; they go through _state_dir() / _id_map_path()
# below, which consult the ContextVar set up by the FastAPI worker.
STATE_DIR = Path(__file__).parent.parent / "state"
ID_MAP_PATH = STATE_DIR / "id_map.json"


def _state_dir() -> Path:
    """Per-context state directory. CLI runs → legacy STATE_DIR; UI/
    worker runs → state/<migration_id>/."""
    return _ctx_state_dir()


def _id_map_path() -> Path:
    return _state_dir() / "id_map.json"


# ------------------------------------------------------------------ #
#  Debounced id_map writer                                            #
# ------------------------------------------------------------------ #
#
# Per-item atomic renames don't scale: 563 organizations meant 563 fsync-grade
# disk writes. We coalesce writes here:
#   - _record_mapping() mutates the in-memory dict and increments a counter.
#   - The on-disk file is rewritten every WRITE_EVERY records (crash-safety
#     ceiling) and at every phase boundary via flush_id_map().
#   - atexit.register ensures the final state is flushed on normal exit.
#
# Concurrency: the bookkeeping (`_pending_writes`, `_last_flushed_ref`)
# is partitioned by the ContextVar-bound migration id. Two simultaneous
# migrations in the same process therefore maintain independent
# flush counters and atexit references — neither one's writes can
# clobber the other's, because the on-disk paths also diverge
# (state/<id_a>/id_map.json vs state/<id_b>/id_map.json).

WRITE_EVERY = 25
_write_lock = threading.Lock()
_atexit_registered = False  # process-global: register once, dispatch many.

# Per-migration bookkeeping. Key = migration_id (None for legacy CLI).
_pending_writes: Dict[Optional[str], int] = {}
_last_flushed_ref: Dict[Optional[str], Dict] = {}


# ------------------------------------------------------------------ #
#  State persistence                                                  #
# ------------------------------------------------------------------ #

def load_id_map() -> Dict[str, Dict[str, str]]:
    """
    Load the ID mapping table from disk.
    Returns an empty dict if the file doesn't exist or is malformed
    (rather than crashing the run).

    Reads from the per-context id_map path: legacy `state/id_map.json`
    in CLI mode, `state/<migration_id>/id_map.json` when bound via
    `src.utils.runctx.current_migration_id`.
    """
    path = _id_map_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warn(
                "id_map.json contained non-dict data — starting with empty map."
            )
            return {}
        return data
    except json.JSONDecodeError as exc:
        logger.warn(
            f"id_map.json is malformed ({exc}). Starting with empty map. "
            "Previous mapping state is lost — re-run from phase 1 or restore from backup."
        )
        return {}
    except OSError as exc:
        logger.warn(f"Could not read id_map.json: {exc}. Starting with empty map.")
        return {}


def save_id_map(id_map: Dict) -> None:
    """
    Atomically write id_map to disk using a temp file + rename pattern.
    This prevents a partial write from corrupting the state file on crash.

    Writes to the per-context state directory.
    """
    sdir = _state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    target = sdir / "id_map.json"
    # Write to a temp file in the same directory, then atomically rename
    fd, tmp_path = tempfile.mkstemp(dir=sdir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # ensure_ascii=False keeps non-Latin names (e.g. "São Paulo",
            # "東京") readable in the saved map — matches extractor.py.
            json.dump(id_map, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)  # atomic on POSIX
    except Exception as exc:
        # Clean up temp file if something went wrong
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.warn(f"Failed to save id_map.json: {exc}")
        raise


def _record_mapping(id_map: Dict, category: str,
                    source_id: Any, target_id: Any) -> None:
    """
    Record a source→target ID mapping. Writes to disk are debounced —
    see WRITE_EVERY at the top of this module. Validates IDs are non-null.
    Safe for concurrent callers via _write_lock.

    Bookkeeping is partitioned by the ContextVar-bound migration id so
    concurrent migrations in the same process can't share a flush
    counter or atexit reference.
    """
    if source_id is None or target_id is None:
        logger.warn(
            f"Refusing to record null ID mapping for '{category}': "
            f"source_id={source_id!r}, target_id={target_id!r}"
        )
        return

    global _atexit_registered
    mid = current_migration_id.get()
    with _write_lock:
        id_map.setdefault(category, {})[str(source_id)] = str(target_id)
        _pending_writes[mid] = _pending_writes.get(mid, 0) + 1
        _last_flushed_ref[mid] = id_map  # remember for atexit flush

        if not _atexit_registered:
            atexit.register(_atexit_flush)
            _atexit_registered = True

        if _pending_writes[mid] >= WRITE_EVERY:
            try:
                save_id_map(id_map)
            finally:
                _pending_writes[mid] = 0


def flush_id_map(id_map: Dict) -> None:
    """
    Force-flush the in-memory id_map to disk. Call between resource
    families (phase boundaries, before any external state-dependent
    action like format-target prompts) so a crash never loses more
    than one family's worth of mappings.
    """
    mid = current_migration_id.get()
    with _write_lock:
        if _pending_writes.get(mid, 0) == 0:
            return
        save_id_map(id_map)
        _pending_writes[mid] = 0


def _atexit_flush() -> None:
    """Best-effort save on process exit. Flushes the last-known id_map
    for every migration id that had pending writes when the interpreter
    started shutting down. We rebind the ContextVar per-entry so each
    flush hits its own state directory.
    """
    # Snapshot to avoid mutating the dict while iterating.
    pending = list(_last_flushed_ref.items())
    for mid, id_map in pending:
        if _pending_writes.get(mid, 0) == 0:
            continue
        token = current_migration_id.set(mid)
        try:
            flush_id_map(id_map)
        except Exception:
            pass  # process is exiting — no logger guaranteed
        finally:
            current_migration_id.reset(token)


# ------------------------------------------------------------------ #
#  Generic import driver                                              #
# ------------------------------------------------------------------ #

def import_resource(
    *,
    client: ZendeskClient,
    id_map: Dict,
    source_items: List[Dict],
    resource_key: str,
    list_path: str,
    list_rkey: str,
    create_path: str,
    create_rkey: str,
    create_response_rkey: str,
    delete_path_fn: Callable[[Any], str],
    name_field: str = "name",
    pre_process_fn: Optional[Callable[[Dict, Dict], Optional[Dict]]] = None,
    skip_system: bool = True,
    conflict_mode: str = "skip",
    update_path_fn: Optional[Callable[[Any], str]] = None,
    max_workers: int = 6,
) -> None:
    """
    Generic import loop for a single resource type.

    For each source item:
    1. Optionally skip system/built-in items
    2. Run pre_process_fn (caller-supplied mutation hook)
    3. Strip server-managed fields
    4. Remap IDs using id_map
    5. If target has a resource with the same name/key:
       - conflict_mode='skip': log as skipped and continue
       - conflict_mode='replace': DELETE target resource, then create source version
       - conflict_mode='update': PUT (update) the target resource in-place
    6. POST the cleaned payload to target
    7. Record source_id → target_id in id_map
    """
    # Build a lookup of existing target resources by name
    try:
        existing = client.list_resource(list_path, list_rkey)
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not list existing {resource_key} in target: {exc}. "
            "Skipping collision check — duplicates may be created."
        )
        existing = []

    # Only index items that have a non-empty name to avoid empty-string collisions
    existing_by_name: Dict[str, Dict] = {
        item[name_field]: item
        for item in existing
        if item.get(name_field)  # guard: skip nameless items
    }

    # ---- Pass 1: prep + sequential conflict resolution ----------------- #
    # Conflict purges/updates happen sequentially to avoid racing with the
    # creates that follow (an in-flight delete could 404 a concurrent
    # create). Items that survive prep are queued for the parallel CREATE
    # pass below.
    queued: List[Dict] = []  # each entry: {"source_id", "name", "payload"}
    for item in source_items:
        if not isinstance(item, dict):
            logger.warn(f"Skipping non-dict item in {resource_key}: {type(item)}")
            continue

        source_id = item.get("id")
        item_name = item.get(name_field) or f"<unnamed id={source_id}>"

        if skip_system and item.get("type") in ("system", "builtin"):
            logger.log_skipped(resource_key, source_id, "system/built-in field skipped")
            continue

        if pre_process_fn:
            try:
                item = pre_process_fn(item, id_map)
            except Exception as exc:
                logger.log_failed(
                    resource_key, source_id,
                    f"pre_process_fn raised {type(exc).__name__}: {exc}", item_name
                )
                continue
            if item is None:
                continue

        try:
            payload = strip_source_fields(item)
        except Exception as exc:
            logger.log_failed(resource_key, source_id,
                              f"strip_source_fields failed: {exc}", item_name)
            continue

        try:
            payload = remap_payload(
                payload, id_map, context=f"{resource_key}:{item_name}"
            )
        except RemapError as exc:
            logger.log_failed(resource_key, source_id, str(exc), item_name)
            continue
        except Exception as exc:
            logger.log_failed(resource_key, source_id,
                              f"Unexpected remapper error: {exc}", item_name)
            continue

        # Conflict handling — sequential by design.
        if item_name and item_name in existing_by_name:
            conflict = existing_by_name[item_name]
            conflict_id = conflict.get("id")
            if conflict_id is None:
                logger.warn(
                    f"Conflict detected for {resource_key} '{item_name}' "
                    "but target resource has no ID — skipping."
                )
                if conflict_mode in ("skip", "replace", "update"):
                    logger.log_skipped(
                        resource_key, source_id,
                        f"Target conflict '{item_name}' has no resolvable ID"
                    )
                    continue

            if conflict_mode == "skip":
                logger.log_skipped(
                    resource_key, source_id,
                    f"Target already has a {resource_key} named '{item_name}'"
                )
                continue

            if conflict_mode == "update" and update_path_fn:
                # Updates stay inline — they're rare and need ordering.
                try:
                    update_path = update_path_fn(conflict_id)
                    resp = client.put(update_path, {create_rkey: payload})
                    if resp.get("dry_run"):
                        logger.log_skipped(
                            resource_key, source_id, "dry-run update"
                        )
                        continue
                    target_id = resp.get(create_response_rkey, {}).get("id")
                    if target_id:
                        _record_mapping(
                            id_map, resource_key, source_id, target_id
                        )
                        logger.log_created(
                            resource_key, source_id, target_id,
                            f"{item_name} (updated)"
                        )
                    else:
                        logger.log_failed(
                            resource_key, source_id,
                            "Update response had no 'id' field.",
                            item_name,
                        )
                    continue
                except (ZendeskAPIError, ZendeskNetworkError) as exc:
                    logger.log_failed(
                        resource_key, source_id,
                        f"Failed to update conflicting target "
                        f"(id={conflict_id}): {exc}",
                        item_name,
                    )
                    continue

            if conflict_mode == "replace":
                try:
                    delete_path = delete_path_fn(conflict_id)
                    client.delete(delete_path)
                    logger.log_purged(resource_key, conflict_id, item_name)
                except (ZendeskAPIError, ZendeskNetworkError) as exc:
                    logger.log_failed(
                        resource_key, source_id,
                        f"Failed to purge conflicting target resource "
                        f"(id={conflict_id}): {exc}",
                        item_name,
                    )
                    continue

        queued.append({
            "source_id": source_id,
            "name": item_name,
            "payload": payload,
        })

    if not queued:
        return

    # ---- Pass 2: parallel CREATE ------------------------------------- #
    # The rate limiter (client.rate_limiter) serializes token spending
    # across threads, so the effective throughput stays under the account's
    # documented RPM no matter how many workers we run. We default to 6
    # workers — enough to hide the per-request HTTP round-trip latency at
    # Suite Team (200 rpm = 1 req per 300ms) without pinning CPU.
    def _create_one(job: Dict) -> None:
        source_id = job["source_id"]
        item_name = job["name"]
        payload = job["payload"]
        try:
            resp = client.post(create_path, {create_rkey: payload})
            if resp.get("dry_run"):
                logger.log_skipped(resource_key, source_id, "dry-run")
                return

            created = resp.get(create_response_rkey)
            if not isinstance(created, dict):
                logger.log_failed(
                    resource_key, source_id,
                    f"Unexpected create response shape — "
                    f"'{create_response_rkey}' key missing or not a dict. "
                    f"Raw keys: {list(resp.keys())}",
                    item_name,
                )
                return

            target_id = created.get("id")
            if target_id is None:
                logger.log_failed(
                    resource_key, source_id,
                    "Create succeeded but response contained no 'id' field.",
                    item_name,
                )
                return

            _record_mapping(id_map, resource_key, source_id, target_id)
            logger.log_created(resource_key, source_id, target_id, item_name)

        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed(resource_key, source_id, str(exc), item_name)
        except Exception as exc:
            logger.log_failed(
                resource_key, source_id,
                f"Unexpected error during create: {type(exc).__name__}: {exc}",
                item_name,
            )

    workers = max(1, min(max_workers, len(queued)))
    if workers == 1:
        # Avoid ThreadPool overhead for trivially-small batches.
        for job in queued:
            _create_one(job)
        return

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() forces all submissions to enqueue (no laziness) and waits
        # for every result. Exceptions inside _create_one are caught there,
        # so map() won't propagate.
        list(pool.map(_create_one, queued))
