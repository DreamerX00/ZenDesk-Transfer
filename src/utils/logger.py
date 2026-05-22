"""
Migration logger — writes structured JSONL entries to state/migration_log.jsonl
and surfaces rich console output.

Bug fixes:
  - _append() now handles OSError (disk full, permissions) gracefully:
    logs to stderr rather than crashing the calling phase.
  - print_table uses List[dict] from typing for Python 3.8 compatibility.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich import box

from src.utils.runctx import (
    DEFAULT_STATE_DIR,
    emit_event,
    migration_log_path,
)

# Kept for backwards compatibility with anything that imports LOG_PATH
# directly. The per-context resolver below is what actually gets used.
LOG_PATH = DEFAULT_STATE_DIR / "migration_log.jsonl"

console = Console()
_err_console = Console(stderr=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(record: Dict) -> None:
    """
    Append a JSONL record to the migration log file.
    If the log file cannot be written (disk full, permissions denied, etc.)
    the error is printed to stderr but does NOT propagate — a logging failure
    must never abort an in-progress migration.

    Also forwards the record to any event sink bound in the current
    ContextVar so an HTTP server / SSE stream can observe the same
    events the CLI sees on its terminal.
    """
    path = migration_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        _err_console.print(
            f"[bold red]LOGGER ERROR[/bold red]: could not write to "
            f"{path}: {exc}"
        )
    except Exception as exc:
        # json.dumps can fail on non-serializable types; default=str handles
        # most cases, but guard for any residual edge case
        _err_console.print(
            f"[bold red]LOGGER ERROR[/bold red]: failed to serialize log record: {exc}"
        )

    # Fan out to the bound sink (no-op when nothing is listening).
    emit_event(record)


# ------------------------------------------------------------------ #
#  Public logging functions                                           #
# ------------------------------------------------------------------ #

def log_created(resource: str, source_id: Any, target_id: Any,
                name: str = "") -> None:
    _append({
        "ts": _now(), "action": "CREATED", "resource": resource,
        "source_id": source_id, "target_id": target_id, "name": name,
    })
    console.print(
        f"  [green]✓ CREATED[/green]  {resource} [dim]{name}[/dim]  "
        f"[cyan]{source_id}[/cyan] → [cyan]{target_id}[/cyan]"
    )


def log_purged(resource: str, target_id: Any, name: str = "") -> None:
    _append({
        "ts": _now(), "action": "PURGED", "resource": resource,
        "target_id": target_id, "name": name,
    })
    console.print(
        f"  [yellow]⚡ PURGED[/yellow]   {resource} [dim]{name}[/dim]  "
        f"target_id=[cyan]{target_id}[/cyan]"
    )


def log_skipped(resource: str, source_id: Any, reason: str = "") -> None:
    _append({
        "ts": _now(), "action": "SKIPPED", "resource": resource,
        "source_id": source_id, "reason": reason,
    })
    console.print(
        f"  [dim]– SKIPPED    {resource} source_id={source_id}  {reason}[/dim]"
    )


def log_failed(resource: str, source_id: Any, error: str,
               name: str = "") -> None:
    # Truncate error string to prevent enormous log lines from full API responses
    safe_error = str(error)[:500]
    _append({
        "ts": _now(), "action": "FAILED", "resource": resource,
        "source_id": source_id, "name": name, "error": safe_error,
    })
    console.print(
        f"  [red]✗ FAILED[/red]    {resource} [dim]{name}[/dim]  "
        f"source_id=[cyan]{source_id}[/cyan]  {safe_error}"
    )


def log_manual(resource: str, note: str) -> None:
    _append({"ts": _now(), "action": "MANUAL", "resource": resource, "note": note})
    console.print(f"  [magenta]⚠ MANUAL[/magenta]    {resource}  {note}")


def _log_event(msg: str, level: str = "info") -> None:
    """Emit an informational event to the JSONL log + event sink so the
    UI event stream sees the same messages the CLI prints to console."""
    _append({"ts": _now(), "action": "NOTE", "resource": level, "note": msg})


def section(title: str) -> None:
    console.rule(f"[bold blue]{title}[/bold blue]")
    _log_event(f"── {title} ──", level="section")


def info(msg: str) -> None:
    console.print(f"[bold white]{msg}[/bold white]")
    _log_event(msg, level="info")


def warn(msg: str) -> None:
    console.print(f"[bold yellow]⚠  {msg}[/bold yellow]")
    _log_event(f"⚠ {msg}", level="warn")


def error(msg: str) -> None:
    console.print(f"[bold red]✗  {msg}[/bold red]")
    _log_event(f"✗ {msg}", level="error")


def success(msg: str) -> None:
    console.print(f"[bold green]✓  {msg}[/bold green]")
    _log_event(f"✓ {msg}", level="success")


def print_table(title: str, rows: List[Dict], columns: List[str]) -> None:
    """
    Render a Rich table from a list of dicts.
    Uses List[Dict] from typing for Python 3.8 compatibility.
    """
    table = Table(title=title, box=box.ROUNDED, highlight=True)
    for col in columns:
        table.add_column(col, style="cyan")
    for row in rows:
        table.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(table)
