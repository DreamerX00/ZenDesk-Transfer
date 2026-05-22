"""
server.state — migration_id allocation, per-migration state directory
helpers, and encrypted credential storage.

migration_id is a 12-char URL-safe slug (`secrets.token_urlsafe(9)`).
It's the only thing the frontend remembers — every later API call is
keyed by it.

Credentials (source + target tokens) are stored in a single Fernet-
encrypted JSON file at `state/connections.enc`. We do NOT use a
database; this is a single-tenant self-hosted backend and the
overhead isn't justified.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from server.settings import get_settings


_MIGRATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_migration_id() -> str:
    """Generate a 12-char URL-safe migration id. Random enough to avoid
    collisions across concurrent runs; short enough to read in a log."""
    return secrets.token_urlsafe(9)  # ~12 chars, no padding


def is_valid_migration_id(mid: str) -> bool:
    return bool(mid) and bool(_MIGRATION_ID_RE.match(mid))


def state_dir_for(migration_id: str) -> Path:
    """Return `state/<migration_id>/`, creating it if absent. Raises
    ValueError if the id fails validation (defence against path
    traversal via crafted ids)."""
    if not is_valid_migration_id(migration_id):
        raise ValueError(f"invalid migration_id: {migration_id!r}")
    p = get_settings().state_root / migration_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def id_map_path_for(migration_id: str) -> Path:
    return state_dir_for(migration_id) / "id_map.json"


def log_path_for(migration_id: str) -> Path:
    return state_dir_for(migration_id) / "migration_log.jsonl"


def report_path_for(migration_id: str) -> Path:
    return state_dir_for(migration_id) / "migration_report.md"


# ------------------------------------------------------------------ #
#  Credential store                                                   #
# ------------------------------------------------------------------ #

@dataclass
class Connection:
    """A stored Zendesk credential."""

    id: str           # uuid-style slug
    role: str         # "source" or "target"
    subdomain: str
    auth_kind: str    # "oauth" or "api_token"
    # One of these is set, depending on auth_kind:
    oauth_token: Optional[str] = None
    oauth_refresh_token: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    email: Optional[str] = None
    api_token: Optional[str] = None
    # Optional metadata
    account_name: Optional[str] = None

    def masked(self) -> Dict:
        """Return a representation safe to send to the iframe — secrets
        are replaced with last-4 chars."""
        def _mask(v: Optional[str]) -> Optional[str]:
            if not v:
                return None
            if len(v) <= 4:
                return "****"
            return "****" + v[-4:]

        return {
            "id": self.id,
            "role": self.role,
            "subdomain": self.subdomain,
            "auth_kind": self.auth_kind,
            "account_name": self.account_name,
            "oauth_token": _mask(self.oauth_token),
            "api_token": _mask(self.api_token),
            "email": self.email,  # email isn't secret
        }


class ConnectionStore:
    """Thread-safe, file-backed, encrypted-at-rest credential store.

    File layout (after Fernet decryption):
        {
          "connections": {
            "<id>": { ...Connection asdict()... }
          }
        }
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path or get_settings().connections_path
        self._fernet = self._build_fernet()
        # In-memory cache. Populated lazily on first access.
        self._cache: Optional[Dict[str, Connection]] = None

    def _build_fernet(self) -> Fernet:
        key = get_settings().fernet_key
        if not key:
            # Ephemeral key — only useful within a single process. The
            # warning was already logged at settings load.
            key = Fernet.generate_key().decode("ascii")
        # Accept either a urlsafe-base64 32-byte Fernet key, or a raw
        # 32-byte hex string we wrap up.
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, base64.binascii.Error):
            # Treat as raw bytes; pad/encode for Fernet.
            try:
                raw = bytes.fromhex(key)
            except ValueError as exc:
                raise ValueError(
                    "ZDX_FERNET_KEY must be a Fernet key "
                    "(urlsafe-base64 of 32 bytes) or a 64-char hex string"
                ) from exc
            if len(raw) != 32:
                raise ValueError(
                    "ZDX_FERNET_KEY must decode to exactly 32 bytes"
                )
            return Fernet(base64.urlsafe_b64encode(raw))

    def _load(self) -> Dict[str, Connection]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            blob = self._path.read_bytes()
            plain = self._fernet.decrypt(blob)
            data = json.loads(plain.decode("utf-8"))
            conns_raw = data.get("connections", {})
            self._cache = {
                cid: Connection(**raw) for cid, raw in conns_raw.items()
            }
        except (InvalidToken, json.JSONDecodeError) as exc:
            # Corrupt or wrong-key file. We refuse to silently overwrite
            # — the operator must investigate. The HTTP layer will turn
            # this into a 500 with an actionable message.
            raise RuntimeError(
                f"Failed to read connections store at {self._path}: "
                f"{exc}. The file is encrypted with a different "
                f"ZDX_FERNET_KEY, or is corrupted."
            )
        return self._cache

    def _save(self) -> None:
        assert self._cache is not None
        payload = {
            "connections": {
                cid: asdict(conn) for cid, conn in self._cache.items()
            }
        }
        plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        blob = self._fernet.encrypt(plain)
        # Atomic write: temp file in same dir, then os.replace.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.chmod(tmp, 0o600)  # owner-only — these are secrets
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self, role: Optional[str] = None) -> List[Connection]:
        with self._lock:
            conns = list(self._load().values())
        if role:
            conns = [c for c in conns if c.role == role]
        return conns

    def get(self, conn_id: str) -> Optional[Connection]:
        with self._lock:
            return self._load().get(conn_id)

    def put(self, conn: Connection) -> None:
        with self._lock:
            self._load()[conn.id] = conn
            self._save()

    def delete(self, conn_id: str) -> bool:
        with self._lock:
            cache = self._load()
            if conn_id not in cache:
                return False
            del cache[conn_id]
            self._save()
            return True


# Module-level singleton — created lazily so tests can override settings
# before first use.
_store: Optional[ConnectionStore] = None
_store_lock = threading.Lock()


def get_connection_store() -> ConnectionStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ConnectionStore()
        return _store
