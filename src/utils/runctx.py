"""
runctx — per-migration run context.

The original CLI assumes a single migration is running in a single
process, and uses module-global mutable state to track id_map writes
and route logger output to one file. That breaks the moment two
migrations run concurrently in the same process — the case we hit as
soon as a long-running FastAPI server starts hosting jobs for the new
Zendesk-app UI.

This module introduces two `ContextVar`s that scope per-migration
state to the current async-task / thread context:

  - `current_migration_id`  — opaque short string identifying the run.
    None means "legacy CLI mode": all state lives under the repo-level
    `state/` directory, exactly as before.
  - `event_sink`            — an optional callable that receives every
    structured log record. Used by the FastAPI worker to forward log
    events to an SSE stream / Redis list. None means "no sink": the
    logger writes only to its JSONL file + Rich console, unchanged.

CLI behavior is byte-identical when neither ContextVar is set, which
is the default in every existing entry point. Callers that want
isolation set the context vars *once* at the start of a run (typically
in the worker job wrapper); the existing helpers in `src/importer.py`
and `src/utils/logger.py` consult them on each call.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Dict, Optional

# Repo-root-relative default — matches the legacy hard-coded path in
# src/importer.py so CLI runs see no behavior change.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = _REPO_ROOT / "state"

# The migration id slug. Should be filesystem-safe (a-zA-Z0-9_-),
# generated at job kickoff. None ⇒ legacy single-run CLI mode.
current_migration_id: ContextVar[Optional[str]] = ContextVar(
    "current_migration_id", default=None
)

# Optional record sink. Receives the same dict that logger.py writes to
# the JSONL file. Sink must be non-blocking and exception-safe — the
# logger swallows exceptions raised by the sink so a flaky listener
# never aborts a phase.
EventSink = Callable[[Dict], None]
event_sink: ContextVar[Optional[EventSink]] = ContextVar(
    "event_sink", default=None
)


def state_dir() -> Path:
    """Directory holding `id_map.json` and `migration_log.jsonl` for the
    currently-bound migration. Falls back to the legacy `state/` dir when
    no migration_id is set in the current context.

    Callers should not cache the result across context changes — the
    returned `Path` is valid only for the current call site.
    """
    mid = current_migration_id.get()
    if mid:
        return DEFAULT_STATE_DIR / mid
    return DEFAULT_STATE_DIR


def id_map_path() -> Path:
    """`id_map.json` path for the current run."""
    return state_dir() / "id_map.json"


def migration_log_path() -> Path:
    """`migration_log.jsonl` path for the current run."""
    return state_dir() / "migration_log.jsonl"


def emit_event(record: Dict) -> None:
    """Forward `record` to the bound event sink, if any. Exceptions
    raised by the sink are swallowed — logging must never abort a phase.
    """
    sink = event_sink.get()
    if sink is None:
        return
    try:
        sink(record)
    except Exception:
        # Swallowed by design — telemetry failure must not abort a phase.
        # We intentionally don't log here to avoid recursion if the sink
        # itself is the logger.
        pass
