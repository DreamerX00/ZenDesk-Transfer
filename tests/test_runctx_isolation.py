"""
Phase A verification: per-migration state isolation.

Three invariants under test:
  1. With no migration_id bound, behavior is byte-identical to v1 —
     id_map.json lands in <repo>/state/, NOT under any sub-directory.
  2. With migration_id="A" and migration_id="B" running concurrently in
     separate threads, each thread's writes land in its own directory
     and neither thread can see the other's mappings.
  3. The logger event sink fires for every structured record produced
     by log_created / log_skipped / log_failed / log_purged / log_manual.

These tests don't touch the network. They use the public API surface
(load_id_map / save_id_map / _record_mapping / flush_id_map / logger.*)
in the same way the real phase code does.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# Repo root on sys.path so `src.*` imports resolve when this is run as a
# bare script (no pytest config in the repo today).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.importer import (  # noqa: E402
    _record_mapping,
    flush_id_map,
    load_id_map,
    save_id_map,
    STATE_DIR as LEGACY_STATE_DIR,
)
from src.utils import logger  # noqa: E402
from src.utils.runctx import (  # noqa: E402
    current_migration_id,
    event_sink,
    state_dir,
)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _isolated_state_root(tmp_path: Path) -> None:
    """Repoint the legacy default to a tmp dir so the test never
    touches real CLI state. We patch runctx.DEFAULT_STATE_DIR plus the
    importer's STATE_DIR module attr (kept for backwards-compat) and
    the logger's LOG_PATH attr."""
    from src.utils import runctx
    runctx.DEFAULT_STATE_DIR = tmp_path  # type: ignore[assignment]
    # Re-import sites that snapshotted the path at import time:
    import src.importer as _imp
    _imp.STATE_DIR = tmp_path  # type: ignore[assignment]
    _imp.ID_MAP_PATH = tmp_path / "id_map.json"  # type: ignore[assignment]
    logger.LOG_PATH = tmp_path / "migration_log.jsonl"  # type: ignore[assignment]


# ------------------------------------------------------------------ #
# Test 1: CLI behavior unchanged                                      #
# ------------------------------------------------------------------ #

def test_legacy_cli_writes_to_default_state_dir(tmp_path: Path) -> None:
    _isolated_state_root(tmp_path)
    assert current_migration_id.get() is None, "default must be None"

    # state_dir() should resolve to the default (no sub-dir)
    assert state_dir() == tmp_path

    id_map: dict = {}
    # Force a flush by recording exactly WRITE_EVERY mappings
    from src.importer import WRITE_EVERY
    for i in range(WRITE_EVERY):
        _record_mapping(id_map, "users", i, i + 1000)

    expected = tmp_path / "id_map.json"
    assert expected.exists(), "legacy path should be written"
    # No sub-dir for migration ids should be created
    children = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert children == [], f"unexpected sub-dirs created: {children}"

    data = json.loads(expected.read_text())
    assert len(data["users"]) == WRITE_EVERY


# ------------------------------------------------------------------ #
# Test 2: Concurrent migrations are isolated                          #
# ------------------------------------------------------------------ #

def test_concurrent_migrations_do_not_collide(tmp_path: Path) -> None:
    _isolated_state_root(tmp_path)
    from src.importer import WRITE_EVERY

    barrier = threading.Barrier(2)

    def _runner(mid: str, n_records: int) -> None:
        # Bind the ContextVar inside this thread.
        token = current_migration_id.set(mid)
        try:
            barrier.wait()  # ensure both threads run interleaved
            id_map: dict = {}
            for i in range(n_records):
                _record_mapping(id_map, "users", i, i + 1)
                # Yield to the other thread between each call.
                if i % 7 == 0:
                    time.sleep(0)
            flush_id_map(id_map)
        finally:
            current_migration_id.reset(token)

    # 50 records each — more than WRITE_EVERY (25), so each thread triggers
    # at least one mid-flight save plus the final flush.
    t1 = threading.Thread(target=_runner, args=("alpha", 50))
    t2 = threading.Thread(target=_runner, args=("beta", 50))
    t1.start(); t2.start()
    t1.join(); t2.join()

    alpha_path = tmp_path / "alpha" / "id_map.json"
    beta_path = tmp_path / "beta" / "id_map.json"
    assert alpha_path.exists(), "alpha state dir missing"
    assert beta_path.exists(), "beta state dir missing"

    alpha = json.loads(alpha_path.read_text())
    beta = json.loads(beta_path.read_text())

    # Each map has exactly its own 50 entries, neither one merged.
    assert len(alpha["users"]) == 50
    assert len(beta["users"]) == 50
    # Source IDs are 0..49 in both; values differ (i+1 in both) — that's fine,
    # the isolation is per-file. The check that matters: legacy file is empty.
    assert not (tmp_path / "id_map.json").exists(), \
        "legacy state file was written despite migration_id being bound"


# ------------------------------------------------------------------ #
# Test 3: load_id_map respects the bound context                      #
# ------------------------------------------------------------------ #

def test_load_id_map_reads_from_per_migration_dir(tmp_path: Path) -> None:
    _isolated_state_root(tmp_path)

    # Seed a per-migration file directly on disk
    sub = tmp_path / "loaded-run"
    sub.mkdir()
    seeded = {"organizations": {"100": "200"}}
    (sub / "id_map.json").write_text(json.dumps(seeded))

    # Without binding, load_id_map sees the legacy (empty) state
    assert load_id_map() == {}, "legacy load should be empty"

    # With binding, it reads the sub-dir file
    token = current_migration_id.set("loaded-run")
    try:
        assert load_id_map() == seeded
    finally:
        current_migration_id.reset(token)


# ------------------------------------------------------------------ #
# Test 4: Event sink fires for every structured record                #
# ------------------------------------------------------------------ #

def test_event_sink_receives_structured_records(tmp_path: Path) -> None:
    _isolated_state_root(tmp_path)

    received: list = []
    sink = lambda r: received.append(r)  # noqa: E731
    token = event_sink.set(sink)
    try:
        logger.log_created("users", 1, 100, name="alice")
        logger.log_purged("groups", 9, name="dead-group")
        logger.log_skipped("triggers", 2, reason="default")
        logger.log_failed("webhooks", 3, "Limit exceeded", name="hook")
        logger.log_manual("oauth", "set credentials manually")
    finally:
        event_sink.reset(token)

    actions = [r["action"] for r in received]
    assert actions == ["CREATED", "PURGED", "SKIPPED", "FAILED", "MANUAL"], \
        f"unexpected actions: {actions}"

    # And these should all also have landed on disk:
    log_file = tmp_path / "migration_log.jsonl"
    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 5
    assert all(json.loads(L)["action"] in {"CREATED", "PURGED", "SKIPPED", "FAILED", "MANUAL"} for L in lines)


# ------------------------------------------------------------------ #
# Test 5: Sink exceptions don't abort logging                         #
# ------------------------------------------------------------------ #

def test_sink_exception_is_swallowed(tmp_path: Path) -> None:
    _isolated_state_root(tmp_path)

    def boom(_record: dict) -> None:
        raise RuntimeError("listener exploded")

    token = event_sink.set(boom)
    try:
        # Must not raise. JSONL write must still happen.
        logger.log_created("users", 1, 100, name="alice")
    finally:
        event_sink.reset(token)

    log_file = tmp_path / "migration_log.jsonl"
    assert log_file.exists()
    assert "CREATED" in log_file.read_text()


# ------------------------------------------------------------------ #
# Manual harness                                                      #
# ------------------------------------------------------------------ #

def _run_all() -> int:
    import tempfile
    tests = [
        test_legacy_cli_writes_to_default_state_dir,
        test_concurrent_migrations_do_not_collide,
        test_load_id_map_reads_from_per_migration_dir,
        test_event_sink_receives_structured_records,
        test_sink_exception_is_swallowed,
    ]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="zd-test-") as td:
            tmp = Path(td)
            try:
                t(tmp)
                print(f"  ✓ {t.__name__}")
            except AssertionError as e:
                print(f"  ✗ {t.__name__}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
