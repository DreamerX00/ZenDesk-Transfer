"""
get_oauth_token.py — One-shot Zendesk OAuth 2.0 Authorization Code flow helper.

Usage:
  # Source account (read-only):
  python get_oauth_token.py --role source \\
                            --subdomain <source-subdomain> \\
                            --client-id zd-transfer-migration \\
                            --secret <client-secret> \\
                            --env config/source.env

  # Target account (maximal write permissions — the default for --role target):
  python get_oauth_token.py --role target \\
                            --subdomain <target-subdomain> \\
                            --client-id zd-transfer-migration \\
                            --secret <client-secret> \\
                            --env config/target.env

What it does:
  1. Opens your browser to the Zendesk authorization page.
  2. The browser redirects to http://localhost/callback?code=... — which
     hits a port nothing is listening on. That's expected; you just copy
     the full redirect URL from your browser's address bar and paste it
     into the terminal when prompted. (Using port 80 requires root to
     bind, so we avoid running a server entirely.)
  3. Exchanges the code for a Bearer token (no expiry unless revoked).
  4. Writes ZENDESK_OAUTH_TOKEN into the target .env file.
     If ZENDESK_EMAIL and ZENDESK_API_TOKEN are still present they are
     commented out so only one auth method is active.

Scopes:
  --role source  →  "read"
      Read-only access to GET endpoints. The migration only pulls data from
      source; no writes ever happen there.
  --role target  →  "read write hc:write"
      Full read+write across all Support resources plus Help Center
      write. This is the broadest set the migration needs and avoids
      mid-run 403s from missing resource-scoped permissions.

The Zendesk admin who clicks "Allow" must still have the underlying account
role (typically Admin) to use those scopes — scopes cannot exceed the
user's Zendesk permissions.

Security:
  - The client secret is never written to any file.
  - The bearer token is written only to the .env file you specify.
  - The local server accepts exactly one request then shuts down.
"""

import argparse
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

import requests  # already in requirements.txt

# ------------------------------------------------------------------ #
REDIRECT_URI   = "http://localhost/callback"  # must match the redirect URI set in Zendesk

# Scope sets, keyed by --role. Source only reads; target needs the broadest
# write surface the migration touches so we don't hit a permission wall
# mid-run.  "read write" already covers Support (tickets, users, groups,
# triggers, views, macros, automations, webhooks, orgs, ticket-fields,
# brands, custom-roles, dynamic-content, schedules, SLAs).  "hc:write"
# adds Help Center (categories, sections, articles, translations).
SCOPES = {
    "source": "read",
    "target": "read write hc:write",
}

TOKEN_ENDPOINT = "https://{subdomain}.zendesk.com/oauth/tokens"
AUTH_ENDPOINT  = (
    "https://{subdomain}.zendesk.com/oauth/authorizations/new"
    "?response_type=code"
    "&redirect_uri={redirect_uri}"
    "&client_id={client_id}"
    "&scope={scope}"
)
# ------------------------------------------------------------------ #


def _build_auth_url(subdomain: str, client_id: str, scope: str) -> str:
    return AUTH_ENDPOINT.format(
        subdomain=urllib.parse.quote(subdomain, safe=""),
        redirect_uri=urllib.parse.quote(REDIRECT_URI, safe=""),
        client_id=urllib.parse.quote(client_id, safe=""),
        scope=urllib.parse.quote(scope, safe=""),
    )


def _exchange_code(
    subdomain: str, client_id: str, secret: str, code: str, scope: str
) -> Tuple[str, Optional[str], str]:
    """Exchange authorization code for a bearer token.

    Returns (access_token, refresh_token, granted_scope) — granted_scope
    is what Zendesk actually issued, which may differ from what we asked
    for if the approving user lacks the underlying account permission.
    refresh_token is None if offline_access was not granted.
    """
    url = TOKEN_ENDPOINT.format(subdomain=subdomain)
    resp = requests.post(
        url,
        json={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     client_id,
            "client_secret": secret,
            "redirect_uri":  REDIRECT_URI,
            "scope":         scope,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"\n✗  Token exchange failed [{resp.status_code}]: {resp.text[:400]}")
        sys.exit(1)

    data = resp.json()
    token = data.get("access_token")
    if not token:
        print(f"\n✗  No access_token in response: {data}")
        sys.exit(1)
    refresh_token = data.get("refresh_token")  # only present with offline_access
    granted = data.get("scope", "")
    return token, refresh_token, granted


def _patch_env(
    env_path: Path, subdomain: str, token: str,
    refresh_token: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> None:
    """
    Write ZENDESK_OAUTH_TOKEN (and optionally refresh/client credentials)
    into the .env file.
    Comments out ZENDESK_EMAIL and ZENDESK_API_TOKEN if present so the
    file doesn't trigger the 'ambiguous credentials' guard.
    Ensures ZENDESK_SUBDOMAIN is set to the correct value.
    """
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    lines = text.splitlines()
    new_lines = []
    oauth_written = False
    refresh_written = False
    client_id_written = False
    client_secret_written = False
    subdomain_written = False

    for line in lines:
        stripped = line.strip()

        # Preserve blank lines and comments that are already comments
        if stripped.startswith("#") or stripped == "":
            new_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()

        if key == "ZENDESK_SUBDOMAIN":
            new_lines.append(f"ZENDESK_SUBDOMAIN={subdomain}")
            subdomain_written = True
        elif key in ("ZENDESK_EMAIL", "ZENDESK_API_TOKEN"):
            # Comment out Basic-Auth keys so only OAuth is active
            new_lines.append(f"# {line}  # disabled: using OAuth")
        elif key == "ZENDESK_OAUTH_TOKEN":
            new_lines.append(f"ZENDESK_OAUTH_TOKEN={token}")
            oauth_written = True
        elif key == "ZENDESK_OAUTH_REFRESH_TOKEN" and refresh_token:
            new_lines.append(f"ZENDESK_OAUTH_REFRESH_TOKEN={refresh_token}")
            refresh_written = True
        elif key == "ZENDESK_CLIENT_ID" and client_id:
            new_lines.append(f"ZENDESK_CLIENT_ID={client_id}")
            client_id_written = True
        elif key == "ZENDESK_CLIENT_SECRET" and client_secret:
            new_lines.append(f"ZENDESK_CLIENT_SECRET={client_secret}")
            client_secret_written = True
        else:
            new_lines.append(line)

    if not subdomain_written:
        new_lines.append(f"ZENDESK_SUBDOMAIN={subdomain}")
    if not oauth_written:
        new_lines.append(f"ZENDESK_OAUTH_TOKEN={token}")
    if refresh_token and not refresh_written:
        new_lines.append(f"ZENDESK_OAUTH_REFRESH_TOKEN={refresh_token}")
    if client_id and not client_id_written:
        new_lines.append(f"ZENDESK_CLIENT_ID={client_id}")
    if client_secret and not client_secret_written:
        new_lines.append(f"ZENDESK_CLIENT_SECRET={client_secret}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ #
#  Main                                                               #
# ------------------------------------------------------------------ #
#
# Note: this script intentionally uses the manual paste-the-redirect-URL
# flow rather than running a local callback server. Reason: the
# REDIRECT_URI registered in Zendesk is `http://localhost/callback`
# (port 80), which requires root to bind. Asking the user to copy the
# code out of the browser URL bar is friction-light and avoids needing
# privileged ports.
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(
        description="Obtain a Zendesk OAuth Bearer token via Authorization Code flow.",
    )
    parser.add_argument(
        "--subdomain", required=True,
        help="Zendesk subdomain (e.g. 'acme' for acme.zendesk.com)",
    )
    parser.add_argument(
        "--client-id", required=True,
        help="OAuth client identifier (e.g. 'zd-transfer-migration')",
    )
    parser.add_argument(
        "--secret", required=True,
        help="OAuth client secret shown after saving the client in Zendesk",
    )
    parser.add_argument(
        "--env", default="config/source.env",
        help="Path to the .env file to update (default: config/source.env)",
    )
    parser.add_argument(
        "--role", choices=sorted(SCOPES.keys()), default="source",
        help=(
            "Which account this token is for. 'source' requests read-only; "
            "'target' requests the broadest write scope the migration needs "
            "('read write hc:write')."
        ),
    )
    parser.add_argument(
        "--scope", default=None,
        help=(
            "Override the OAuth scope string. Advanced — only set this if "
            "you know which resource-scoped permissions you need (e.g. "
            "'read write hc:write impersonate'). Leave unset to use the "
            "default for --role."
        ),
    )
    args = parser.parse_args()

    scope = args.scope or SCOPES[args.role]

    # Accept both "mycompany" and "mycompany.zendesk.com" — strip the suffix
    raw = args.subdomain.strip().lower()
    if raw.endswith(".zendesk.com"):
        raw = raw[: -len(".zendesk.com")]
    # Basic sanity check before building URLs
    import re as _re
    if not _re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,61}[a-z0-9]?", raw):
        print(f"\n✗  Invalid subdomain '{raw}'. "
              "Pass just the slug, e.g. 'dreamer-12487' (not the full URL).")
        sys.exit(1)
    subdomain = raw
    env_path  = Path(args.env)

    # Ensure the parent directory of the .env file (typically `config/`)
    # exists before we try to write the token to it. Prompts y/n on a TTY.
    project_root = Path(__file__).resolve().parent
    try:
        from src.utils.paths import ensure_dirs  # local import to avoid hard dep
    except ImportError:
        ensure_dirs = None
    if ensure_dirs is not None:
        env_dir = env_path.parent if str(env_path.parent) else Path(".")
        if not ensure_dirs([env_dir], project_root=project_root):
            sys.exit(1)
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)

    auth_url = _build_auth_url(subdomain, args.client_id, scope)
    print(f"\n→  Requesting scope: {scope}  (role={args.role})")
    print(f"→  Open this URL in your browser and click Allow:")
    print(f"   {auth_url}\n")
    webbrowser.open(auth_url)

    print("⏳  After authorizing, your browser will redirect to a page that says")
    print("    'localhost refused to connect' — that's expected.")
    print("    Copy the full redirect URL from your browser's address bar and paste it below.\n")

    try:
        redirect_url = input("Paste the full redirect URL here: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n✗  No URL provided.")
        sys.exit(1)

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        error = params["error"][0]
        desc = params.get("error_description", [""])[0]
        print(f"\n✗  Authorization denied: {error} — {desc}")
        sys.exit(1)

    codes = params.get("code")
    if not codes:
        print(f"\n✗  No authorization code found in URL. Query params: {dict(params)}")
        sys.exit(1)

    code = codes[0]
    print("✓  Authorization code received. Exchanging for token…")
    token, refresh_token, granted = _exchange_code(
        subdomain, args.client_id, args.secret, code, scope
    )

    # Verify that Zendesk actually granted what we asked for. If the
    # approving user lacks the underlying account permission, Zendesk will
    # silently issue a token with a *narrower* scope set — and any
    # write/hc:write call later returns 403. Flag the gap loudly here so
    # the user can re-approve with the right account before migrating.
    if granted:
        asked = set(scope.split())
        got   = set(granted.split())
        missing = asked - got
        extra   = got - asked
        print(f"✓  Granted scope:   {granted}")
        if "offline_access" in missing:
            print(
                "   ⚠  Did NOT get offline_access — auto token refresh "
                "will NOT be available.\n"
                "   Re-authorize with an account that can grant offline_access\n"
                "   to enable automatic token refresh on expiry.\n"
            )
            missing.discard("offline_access")
        if missing:
            print(
                f"\n⚠  WARNING — Zendesk did NOT grant: {' '.join(sorted(missing))}\n"
                "   The approving user likely doesn't have permission for these.\n"
                "   The migration may fail with 403 on related endpoints.\n"
                "   Re-run this script after the admin approves with an account\n"
                "   that has the required role.\n"
            )
        elif extra:
            extra_display = sorted(e for e in extra if e != "offline_access")
            if extra_display:
                print(f"   (Zendesk also granted: {' '.join(extra_display)})")

    print(f"✓  Token obtained. Writing to '{env_path}'…")
    _patch_env(
        env_path, subdomain, token,
        refresh_token=refresh_token,
        client_id=args.client_id,
        client_secret=args.secret,
    )

    if refresh_token:
        print(   "   ✓  Refresh token stored — auto-refresh is enabled.")
    else:
        print(   "   ⚠  No refresh token received. Auto-refresh will NOT be available.")
    print(f"\n✅  Done! ZENDESK_OAUTH_TOKEN written to '{env_path}'.")
    print(   "   Run 'python main.py pre-flight' to verify the connection.\n")


if __name__ == "__main__":
    main()
