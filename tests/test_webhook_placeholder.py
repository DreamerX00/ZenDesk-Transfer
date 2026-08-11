"""
Tests for the webhook placeholder-credential workaround: write-only-auth
webhooks are now RECREATED (with placeholder secrets) instead of skipped, so
triggers that notify them can still be migrated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh(tmp: Path) -> None:
    from server import settings as s_mod
    from server import state as st_mod
    os.environ["ZDX_DEV_MODE"] = "1"
    os.environ["ZDX_STATE_ROOT"] = str(tmp)
    os.environ["ZDX_CONNECTIONS_PATH"] = str(tmp / "connections.enc")
    s_mod._settings = None
    st_mod._store = None


def test_bearer_token_gets_placeholder_not_skipped(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _scrub_webhook_secret

    item = {
        "name": "wh1", "endpoint": "https://x.example/hook",
        "authentication": {"type": "bearer_token", "data": {}},  # secret not returned
        "signing_secret": {"secret": "abc"},
    }
    out = _scrub_webhook_secret(item, {})
    assert out is not None  # NOT skipped
    assert out["authentication"]["type"] == "bearer_token"
    assert out["authentication"]["data"]["token"] == "PLACEHOLDER_UPDATE_ME"
    assert "signing_secret" not in out  # stripped so Zendesk mints a new one


def test_basic_auth_placeholder(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _scrub_webhook_secret

    item = {"name": "wh2", "authentication": {"type": "basic_auth", "data": {}}}
    out = _scrub_webhook_secret(item, {})
    data = out["authentication"]["data"]
    assert data["username"] == "placeholder"
    assert data["password"] == "PLACEHOLDER_UPDATE_ME"


def test_api_key_keeps_returned_name(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _scrub_webhook_secret

    # api_key auth returns the header NAME but not the value.
    item = {"name": "wh3", "authentication": {"type": "api_key", "data": {"name": "X-Custom"}}}
    out = _scrub_webhook_secret(item, {})
    data = out["authentication"]["data"]
    assert data["name"] == "X-Custom"           # preserved
    assert data["value"] == "PLACEHOLDER_UPDATE_ME"  # secret placeheld


def test_signing_secret_type_still_migrates(tmp_path: Path) -> None:
    _fresh(tmp_path)
    from src.phases.phase2_business_logic import _scrub_webhook_secret

    item = {"name": "wh4", "authentication": {"type": "signing_secret"},
            "signing_secret": {"secret": "s"}}
    out = _scrub_webhook_secret(item, {})
    assert out is not None
    assert "signing_secret" not in out
