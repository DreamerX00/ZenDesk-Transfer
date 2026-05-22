"""
test_create_user.py — sanity-check whether Zendesk sends a verification
email when we create a user via the API.

Creates two end-users on the TARGET account:
  • <local>+zd-default@<domain>   — minimal payload, Zendesk decides
                                     (expectation: verification email sent)
  • <local>+zd-verified@<domain>  — payload sets `verified: true`
                                     (expectation: no email sent)

Both addresses are plus-aliases of the address you pass in, so a single
inbox receives whatever Zendesk decides to send.

After the run, check the inbox for the base address — count of emails
received tells you exactly which path triggered mail.

Usage:
  python test_create_user.py --email you@example.com
  python test_create_user.py --email you@example.com --target config/target.env

The script reuses the existing ZendeskClient (same auth, same rate
limiter) and posts to /api/v2/users.json. It cleans up after itself by
default — pass --keep to leave the test users in place.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Make `src` importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import dotenv_values

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.utils import logger


def _build_client(env_path: Path) -> ZendeskClient:
    """Tiny stand-in for main.py's _load_client — same precedence rules."""
    if not env_path.exists():
        raise SystemExit(f"Credentials file not found: '{env_path}'")
    cfg = dotenv_values(str(env_path))
    subdomain   = (cfg.get("ZENDESK_SUBDOMAIN") or "").strip()
    oauth_token = (cfg.get("ZENDESK_OAUTH_TOKEN") or "").strip()
    email       = (cfg.get("ZENDESK_EMAIL") or "").strip()
    api_token   = (cfg.get("ZENDESK_API_TOKEN") or "").strip()
    if not subdomain:
        raise SystemExit(f"ZENDESK_SUBDOMAIN missing in '{env_path}'")
    if oauth_token and (email or api_token):
        raise SystemExit(
            f"Ambiguous credentials in '{env_path}': "
            "set OAuth OR email+token, not both."
        )
    if oauth_token:
        return ZendeskClient(subdomain=subdomain, oauth_token=oauth_token)
    if not (email and api_token):
        raise SystemExit(
            f"'{env_path}' is missing ZENDESK_EMAIL / ZENDESK_API_TOKEN "
            "(or ZENDESK_OAUTH_TOKEN for OAuth)."
        )
    return ZendeskClient(subdomain=subdomain, email=email, api_token=api_token)


def _alias(email: str, tag: str) -> str:
    """Turn 'you@example.com' into 'you+tag@example.com'."""
    if "@" not in email:
        raise SystemExit(f"--email must be a valid address, got: {email!r}")
    local, _, domain = email.partition("@")
    if "+" in local:
        # Strip any existing tag so we don't end up with you+old+new@...
        local = local.split("+", 1)[0]
    return f"{local}+{tag}@{domain}"


def _create_user(client: ZendeskClient, payload: dict) -> dict:
    """POST /api/v2/users.json and return the user dict on success."""
    resp = client.post("users.json", {"user": payload})
    return resp.get("user") or {}


def _delete_user(client: ZendeskClient, user_id) -> None:
    """Soft-delete the user. Best-effort — failures are logged, not raised."""
    try:
        client.delete(f"users/{user_id}")
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(f"  Could not delete test user {user_id}: {exc}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Create two test users on the target Zendesk account "
                    "and report whether each was expected to trigger an email."
    )
    p.add_argument(
        "--email", required=True,
        help="Base email address. Plus-aliases (you+tag@domain) are "
             "generated from this so a single inbox receives any mail "
             "Zendesk sends.",
    )
    p.add_argument(
        "--target", default="config/target.env",
        help="Path to the target account .env (default: config/target.env)",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Don't delete the test users after creation. Default is to "
             "clean them up so the target stays tidy.",
    )
    args = p.parse_args()

    client = _build_client(Path(args.target))

    # Make sure we're talking to the right account before creating anything.
    me = client.ping()
    logger.info(
        f"Target: {me['account']['name']} "
        f"({me['account']['subdomain']}.zendesk.com)"
    )

    email_default  = _alias(args.email, "zd-default")
    email_verified = _alias(args.email, "zd-verified")

    cases = [
        {
            "label": "DEFAULT (Zendesk decides)",
            "expectation": "verification email EXPECTED",
            "payload": {
                "name":  "ZD Migration Test (default)",
                "email": email_default,
                "role":  "end-user",
            },
        },
        {
            "label": "verified=true (suppressed)",
            "expectation": "NO email expected",
            "payload": {
                "name":     "ZD Migration Test (verified)",
                "email":    email_verified,
                "role":     "end-user",
                "verified": True,
            },
        },
    ]

    created = []
    logger.section("Creating test users")
    for case in cases:
        print()
        logger.info(f"→  {case['label']}")
        logger.info(f"   {case['expectation']}")
        logger.info(f"   email: {case['payload']['email']}")
        try:
            user = _create_user(client, case["payload"])
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(f"   ✗  Create failed: {exc}")
            continue

        uid = user.get("id")
        logger.info(
            f"   ✓  Created id={uid}  "
            f"verified={user.get('verified')!r}  "
            f"role={user.get('role')!r}"
        )
        created.append((case["label"], user))

    if not created:
        logger.warn("No users were created — nothing to clean up.")
        return 1

    # Give Zendesk a moment to actually send (or not send) the mail before
    # we hand control back. Useful when the inbox check happens immediately.
    print()
    logger.info("Waiting 10 s so any outbound mail has time to land...")
    time.sleep(10)

    print()
    logger.section("Inbox check")
    print(
        "  Open the inbox for the base address you provided and look for:\n"
        f"    • Mail to {email_default}   ← should arrive (Zendesk verification)\n"
        f"    • Mail to {email_verified}  ← should NOT arrive (verified:true suppresses)\n"
        "  Gmail/Fastmail/etc. group plus-aliases into the same inbox, so\n"
        "  both will show up there if Zendesk sent them.\n"
    )

    if args.keep:
        print()
        logger.info("--keep set — leaving test users in place.")
        for label, u in created:
            logger.info(f"  • {label}: id={u.get('id')}  email={u.get('email')}")
        return 0

    print()
    logger.section("Cleanup")
    for label, u in created:
        uid = u.get("id")
        if uid is None:
            continue
        logger.info(f"  Deleting {label} (id={uid})...")
        _delete_user(client, uid)
    logger.success("Test users removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
