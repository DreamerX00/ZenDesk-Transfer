"""
server.events — event-bus glue between the per-job event sink (set in
the worker process via `src.utils.runctx.event_sink`) and the SSE
endpoint the iframe subscribes to.

Mechanics: the worker pushes structured event records onto a Redis
list per migration_id (`events:<mid>`). The HTTP server's SSE endpoint
LPOPs (actually BLPOPs) from that list and streams to the iframe.

A parallel `status:<mid>` Redis hash tracks the rollup metrics
(phase, completed, total, errors) that the polling endpoint
`/jobs/{id}/status` returns. Updating it on each event lets the
iframe show a coarse progress bar without having to replay every
event.

Both keys expire 7 days after the last write — finished migrations
don't accumulate forever.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, Optional

import redis  # type: ignore[import-untyped]

from server.settings import get_settings


_EVENT_TTL_SECONDS = 7 * 24 * 3600


def _redis() -> "redis.Redis":
    """Module-level connection. The redis-py client is thread-safe and
    pools internally; we don't need to share an explicit pool."""
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def _events_key(migration_id: str) -> str:
    return f"zdx:events:{migration_id}"


def _status_key(migration_id: str) -> str:
    return f"zdx:status:{migration_id}"


def _cancel_key(migration_id: str) -> str:
    return f"zdx:cancel:{migration_id}"


# ------------------------------------------------------------------ #
#  Producer side (worker)                                             #
# ------------------------------------------------------------------ #

def make_event_sink(migration_id: str):
    """Return a callable suitable for `src.utils.runctx.event_sink.set()`.

    Pushes each record onto the migration's Redis list as JSON, and
    updates the rollup hash. Safe to call from any thread; redis-py
    handles concurrency. Failures are swallowed (logged to stderr) —
    a flaky Redis must NOT abort a migration.
    """
    import sys
    r = _redis()
    ekey = _events_key(migration_id)
    skey = _status_key(migration_id)

    def _sink(record: Dict) -> None:
        try:
            line = json.dumps(record, default=str)
            pipe = r.pipeline()
            pipe.rpush(ekey, line)
            pipe.expire(ekey, _EVENT_TTL_SECONDS)
            # Per-action counter for the rollup display.
            action = record.get("action", "?")
            pipe.hincrby(skey, f"count:{action.lower()}", 1)
            pipe.hset(skey, "last_event_ts", record.get("ts", ""))
            pipe.expire(skey, _EVENT_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            print(
                f"[event_sink] failed to record event for {migration_id}: {exc}",
                file=sys.stderr,
            )

    return _sink


def set_status(migration_id: str, **fields: Any) -> None:
    """Worker calls this at phase boundaries to update the rollup hash
    that `/jobs/{id}/status` reads. Values are stringified by redis-py.
    """
    if not fields:
        return
    try:
        r = _redis()
        r.hset(_status_key(migration_id), mapping={
            k: ("" if v is None else str(v)) for k, v in fields.items()
        })
        r.expire(_status_key(migration_id), _EVENT_TTL_SECONDS)
    except Exception as exc:
        import sys
        print(f"[set_status] failed for {migration_id}: {exc}", file=sys.stderr)


def is_cancelled(migration_id: str) -> bool:
    """Worker polls this at phase boundaries; returns True if the iframe
    POSTed /jobs/{id}/cancel. Network failure → False (fail-open: a
    momentary Redis blip should not pre-emptively abort a healthy
    migration)."""
    try:
        return _redis().exists(_cancel_key(migration_id)) == 1
    except Exception:
        return False


# ------------------------------------------------------------------ #
#  Consumer side (HTTP)                                               #
# ------------------------------------------------------------------ #

def get_status(migration_id: str) -> Dict[str, Any]:
    """Return the rollup hash for `/jobs/{id}/status`. Empty dict when
    the migration id is unknown."""
    try:
        raw = _redis().hgetall(_status_key(migration_id))
        return raw or {}
    except Exception as exc:
        return {"error": f"redis unavailable: {exc}"}


def get_log_tail(migration_id: str, n: int = 50) -> list:
    """Return the last `n` events for this migration. Used by
    `/jobs/{id}/status` so the iframe can show a tail even if it just
    opened the page."""
    try:
        r = _redis()
        raw = r.lrange(_events_key(migration_id), -n, -1)
        out = []
        for line in raw:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out
    except Exception:
        return []


def request_cancel(migration_id: str) -> None:
    """Set the cancel flag the worker polls."""
    try:
        r = _redis()
        r.set(_cancel_key(migration_id), "1", ex=_EVENT_TTL_SECONDS)
    except Exception as exc:
        import sys
        print(f"[request_cancel] failed for {migration_id}: {exc}", file=sys.stderr)


async def stream_events(
    migration_id: str,
    *,
    poll_interval: float = 0.5,
    idle_timeout: float = 30.0,
) -> AsyncIterator[str]:
    """Async generator yielding SSE-encoded event lines.

    Implementation note: we deliberately do NOT use redis blocking
    pop (BLPOP) — that would force one Redis connection per concurrent
    SSE client, which doesn't scale. Instead we poll the list length
    every `poll_interval` seconds and only fetch new tail entries since
    last cursor. This is fine because event volume is moderate
    (typically <10/sec even at peak) and Redis LRANGE is O(N) with
    tiny constants.
    """
    import asyncio

    r = _redis()
    cursor = 0  # last index already streamed (0-based, inclusive)
    last_emit = time.monotonic()

    # Yield the initial back-log first so a freshly-opened tab catches up.
    try:
        backlog = r.lrange(_events_key(migration_id), 0, -1)
    except Exception:
        backlog = []
    for line in backlog:
        yield f"data: {line}\n\n"
        cursor += 1
        last_emit = time.monotonic()

    # Then poll for new entries until the status flips to a terminal state.
    while True:
        try:
            current_len = r.llen(_events_key(migration_id))
        except Exception:
            await asyncio.sleep(poll_interval)
            continue

        if current_len > cursor:
            try:
                new = r.lrange(_events_key(migration_id), cursor, current_len - 1)
            except Exception:
                new = []
            for line in new:
                yield f"data: {line}\n\n"
            cursor = current_len
            last_emit = time.monotonic()

        # Terminal state check — does the worker think this job is done?
        try:
            phase = r.hget(_status_key(migration_id), "phase") or ""
        except Exception:
            phase = ""
        if phase in ("completed", "failed", "cancelled"):
            # Drain anything that landed after the phase flip.
            try:
                final_len = r.llen(_events_key(migration_id))
                if final_len > cursor:
                    tail = r.lrange(_events_key(migration_id), cursor, final_len - 1)
                    for line in tail:
                        yield f"data: {line}\n\n"
            except Exception:
                pass
            yield f"event: done\ndata: {phase}\n\n"
            return

        # Heartbeat so proxies don't time out an idle connection.
        if time.monotonic() - last_emit > idle_timeout:
            yield ": heartbeat\n\n"
            last_emit = time.monotonic()

        await asyncio.sleep(poll_interval)
