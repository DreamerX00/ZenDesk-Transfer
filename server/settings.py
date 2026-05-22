"""
server.settings — single source of truth for backend runtime configuration.

Read once at import time. Validation failures are surfaced loudly so a
mis-configured Docker container exits at boot instead of crashing at the
first HTTP request.

All values come from environment variables. Sensible dev defaults are
provided so `uvicorn server.app:app` works against a local Redis without
any extra env var setup.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    """Snapshot of process configuration.

    Every default is a `default_factory` (not a bare expression) so the
    env var is re-read each time `Settings()` is constructed. That
    matters because `get_settings()` caches the instance and tests
    reset the cache after mutating `os.environ`.
    """

    # --- HTTP -----------------------------------------------------------
    host: str = field(default_factory=lambda: os.environ.get("ZDX_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("ZDX_PORT", "8080")))
    backend_url: str = field(default_factory=lambda:
        os.environ.get("ZDX_BACKEND_URL", "http://localhost:8080").rstrip("/"))

    # CORS — locked to a customer's Zendesk subdomain in production. The
    # iframe's origin is `https://<subdomain>.zendesk.com` while running
    # in-app, and `https://localhost:4567` while developing with
    # `zcli apps:server`. Wildcards are intentionally NOT supported.
    cors_origins: List[str] = field(default_factory=lambda: _env_list(
        "ZDX_CORS_ORIGINS",
        ["https://localhost:4567", "http://localhost:4567"],
    ))

    # --- Redis / RQ -----------------------------------------------------
    redis_url: str = field(default_factory=lambda:
        os.environ.get("ZDX_REDIS_URL", "redis://localhost:6379/0"))
    rq_queue_name: str = field(default_factory=lambda:
        os.environ.get("ZDX_QUEUE_NAME", "zd-transfer"))
    rq_job_timeout: int = field(default_factory=lambda:
        int(os.environ.get("ZDX_JOB_TIMEOUT_S", "21600")))  # 6 hrs

    # --- Secrets --------------------------------------------------------
    # HMAC the iframe uses to sign /session calls. MUST be set in
    # production; we generate a per-process ephemeral one in dev so the
    # server still boots, but it changes on every restart (intentional —
    # forces operator to set it explicitly before shipping).
    hmac_secret: str = field(default_factory=lambda:
        os.environ.get("ZDX_HMAC_SECRET") or secrets.token_hex(32))

    # Symmetric key for at-rest encryption of stored credentials. Same
    # rule as hmac_secret: ephemeral in dev, required in production.
    fernet_key: str = field(default_factory=lambda:
        os.environ.get("ZDX_FERNET_KEY", ""))

    # --- State paths ---------------------------------------------------
    state_root: Path = field(default_factory=lambda:
        Path(os.environ.get("ZDX_STATE_ROOT", str(_REPO_ROOT / "state"))))
    connections_path: Path = field(default_factory=lambda: Path(
        os.environ.get("ZDX_CONNECTIONS_PATH", str(_REPO_ROOT / "state" / "connections.enc"))
    ))

    # --- Behaviour flags ----------------------------------------------
    require_hmac: bool = field(default_factory=lambda: _env_bool("ZDX_REQUIRE_HMAC", True))
    dev_mode: bool = field(default_factory=lambda: _env_bool("ZDX_DEV_MODE", False))

    # Standalone mode: serve the React UI directly from the backend
    # without the Zendesk-iframe wrapper. The /api/v1/standalone/session
    # endpoint mints a session without HMAC. Convenient for a self-
    # hosted setup where the operator drives the migration from a
    # browser on the same machine. **Disabled by default** — production
    # deployments behind the Zendesk app should leave this off.
    standalone_mode: bool = field(default_factory=lambda: _env_bool("ZDX_STANDALONE_MODE", False))

    # Directory holding the built UI bundle (iframe.html + assets).
    # Empty string = UI serving disabled.
    ui_dir: str = field(default_factory=lambda: os.environ.get("ZDX_UI_DIR", ""))


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Lazy singleton. Tests can monkey-patch the module-level _settings
    to inject overrides without restarting the process."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _validate(_settings)
    return _settings


def _validate(s: Settings) -> None:
    # In production we require explicit secrets.
    if not s.dev_mode:
        if not os.environ.get("ZDX_HMAC_SECRET"):
            # Don't crash — warn loudly. The auto-generated value still
            # provides security within a single boot.
            import sys
            print(
                "WARNING: ZDX_HMAC_SECRET is unset; using an ephemeral value "
                "that changes on every restart. Set this in production.",
                file=sys.stderr,
            )
        if not s.fernet_key:
            import sys
            print(
                "WARNING: ZDX_FERNET_KEY is unset; stored credentials will "
                "use an ephemeral key and become unreadable across restarts. "
                "Generate one with `python -m cryptography.fernet`.",
                file=sys.stderr,
            )

    # Sanity checks regardless of mode.
    if not (1 <= s.port <= 65535):
        raise ValueError(f"ZDX_PORT out of range: {s.port}")
    if s.rq_job_timeout < 60:
        raise ValueError(f"ZDX_JOB_TIMEOUT_S too short: {s.rq_job_timeout}")
