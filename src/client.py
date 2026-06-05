"""
ZendeskClient — authenticated HTTP client with automatic pagination,
rate-limit back-off, and timeout handling.

Authentication methods (mutually exclusive):
  1. Email + API Token  — Basic Auth with '/token' suffix (default Zendesk method).
     Set ZENDESK_EMAIL and ZENDESK_API_TOKEN in the .env file.
  2. OAuth Token        — Bearer token issued via Zendesk OAuth 2.0 flow.
     Set ZENDESK_OAUTH_TOKEN in the .env file.  Email/API-token are NOT required.

Security notes:
  - Credentials are passed at construction time and never logged.
  - The _url() method validates that next_page URLs belong to the same
    Zendesk subdomain to prevent open-redirect / SSRF attacks.
  - All response bodies are safely decoded with explicit error handling.
  - URLs containing query strings are never logged in full to prevent
    accidental token leakage.
"""

import io
import re
import threading
import time
import urllib.parse
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout, RequestException
from typing import Any, Dict, Generator, List, Optional


class _RateLimiter:
    """
    Token-bucket throttle calibrated to the target account's documented
    requests-per-minute ceiling.

    Zendesk publishes the account's effective RPM in every response via the
    `X-Rate-Limit` header (e.g. "700" on Enterprise, "200" on Team). We seed
    the bucket with a conservative default and recalibrate on the first
    response — and on every response thereafter, so a plan change mid-run
    or a temporary throttle (anti-abuse cap) is picked up automatically.

    Refill is continuous: at RPM=700 the bucket gains 700/60 ≈ 11.67 tokens
    per second. `acquire()` waits if the bucket is empty, then deducts one
    token. Safe for use from multiple threads via an internal lock.

    `x-rate-limit-remaining` is consulted as a safety net: if the server
    says we have fewer requests left than our local bucket thinks, we
    immediately deflate the bucket to match the server's view.
    """

    # Conservative seed used before we've seen a response. Matches the
    # lowest documented plan (Suite Team / Support Team = 200/min) so a
    # brand-new client never bursts past the smallest plan's ceiling.
    DEFAULT_RPM = 200

    def __init__(self, rpm: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._rpm = float(rpm or self.DEFAULT_RPM)
        self._tokens = float(self._rpm)  # start full
        self._last_refill = time.monotonic()

    @property
    def rpm(self) -> int:
        return int(self._rpm)

    def acquire(self) -> None:
        """Block until at least one token is available, then consume it."""
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / (self._rpm / 60.0) if self._rpm > 0 else 0.05
            # Sleep outside the lock so other threads can recalibrate.
            time.sleep(min(wait, 1.0))

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._rpm, self._tokens + elapsed * (self._rpm / 60.0))
        self._last_refill = now

    def observe_response(self, headers) -> None:
        """
        Recalibrate from response headers. Called after every HTTP response.
        - X-Rate-Limit:           authoritative RPM for the account
        - x-rate-limit-remaining: server-reported remaining requests this minute
        """
        # Header names are case-insensitive in requests' CaseInsensitiveDict
        try:
            new_rpm_str = headers.get("X-Rate-Limit")
            remaining_str = headers.get("x-rate-limit-remaining")
        except AttributeError:
            return

        try:
            new_rpm = int(new_rpm_str) if new_rpm_str else None
        except (TypeError, ValueError):
            new_rpm = None
        try:
            remaining = int(remaining_str) if remaining_str else None
        except (TypeError, ValueError):
            remaining = None

        with self._lock:
            if new_rpm and new_rpm > 0 and abs(new_rpm - self._rpm) >= 1:
                # Rescale tokens proportionally so we don't suddenly burst
                # when discovering a higher ceiling.
                ratio = new_rpm / self._rpm if self._rpm > 0 else 1.0
                self._rpm = float(new_rpm)
                self._tokens = min(self._rpm, self._tokens * ratio)
            if remaining is not None and remaining < self._tokens:
                # Server says we have less budget than our bucket thinks.
                # Trust the server — prevents 429 storms on shared accounts.
                self._tokens = float(remaining)


# ------------------------------------------------------------------ #
#  OAuth token helpers                                                 #
# ------------------------------------------------------------------ #

def _update_env_token(env_path: str, access_token: str,
                      refresh_token: Optional[str] = None) -> None:
    """
    Update ZENDESK_OAUTH_TOKEN (and optionally ZENDESK_OAUTH_REFRESH_TOKEN)
    in the .env file at env_path.  This is a best-effort write — callers
    should never fail because the env file can't be written.
    """
    try:
        path = Path(env_path)
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        new_lines = []
        token_written = False
        refresh_written = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped == "":
                new_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key == "ZENDESK_OAUTH_TOKEN":
                new_lines.append(f"ZENDESK_OAUTH_TOKEN={access_token}")
                token_written = True
            elif key == "ZENDESK_OAUTH_REFRESH_TOKEN" and refresh_token:
                new_lines.append(f"ZENDESK_OAUTH_REFRESH_TOKEN={refresh_token}")
                refresh_written = True
            else:
                new_lines.append(line)
        if not token_written:
            new_lines.append(f"ZENDESK_OAUTH_TOKEN={access_token}")
        if refresh_token and not refresh_written:
            new_lines.append(f"ZENDESK_OAUTH_REFRESH_TOKEN={refresh_token}")
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception:
        pass  # best-effort


class ZendeskAPIError(Exception):
    """Raised when the Zendesk API returns a non-recoverable error."""

    def __init__(self, status_code: int, message: str, url: str = ""):
        # Truncate message to avoid logging full sensitive response bodies
        safe_msg = str(message)[:400]
        super().__init__(f"[{status_code}] {safe_msg}")
        self.status_code = status_code
        # Store only the path portion of the URL, not query string (may contain tokens)
        self.url = urllib.parse.urlparse(url).path if url else ""


class ZendeskNetworkError(Exception):
    """Raised on network-level failures (timeout, DNS, connection refused)."""


class ZendeskClient:
    """
    Thin wrapper around the Zendesk REST API v2.

    Authentication (choose one):
      - email + api_token  → Basic Auth (email/token : api_token)
      - oauth_token        → Bearer token (Authorization: Bearer <token>)
    Pagination:     cursor-based via 'next_page' or 'links.next'.
    Rate limits:    respects Retry-After header; exponential back-off fallback.
    SSRF guard:     next_page URLs are validated against the expected subdomain.
    """

    BASE = "https://{subdomain}.zendesk.com/api/v2"
    MAX_RETRIES = 6
    BACKOFF_BASE = 2   # seconds; wait = BACKOFF_BASE ** attempt (capped at 64s)
    BACKOFF_CAP = 64   # seconds maximum back-off
    REQUEST_TIMEOUT = 30  # seconds

    # Valid Zendesk subdomain: 1-63 lowercase alnum chars or hyphens,
    # must start and end with alnum (same rules as DNS label).
    _SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")
    # Minimal email guard — not full RFC5322, catches obvious injections.
    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def __init__(
        self,
        subdomain: str,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        oauth_token: Optional[str] = None,
        dry_run: bool = False,
        oauth_refresh_token: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        env_path: Optional[str] = None,
    ):
        """
        Construct a ZendeskClient.

        Exactly one auth method must be supplied:
          • email + api_token  — Basic Auth (email/token : api_token)
          • oauth_token        — Bearer token

        When using OAuth, oauth_refresh_token + oauth_client_id + oauth_client_secret
        can be provided to enable automatic token refresh on 401. env_path is the path
        to the .env file that will be updated with the new token after refresh.

        Raises ValueError for invalid inputs or ambiguous/missing auth.
        """
        subdomain = self._sanitize_subdomain(subdomain)

        # ---- auth method selection ----------------------------------------
        using_basic = bool(email or api_token)
        using_oauth = bool(oauth_token)

        if using_basic and using_oauth:
            raise ValueError(
                "Specify either email+api_token (Basic Auth) or oauth_token "
                "(Bearer), not both."
            )
        if not using_basic and not using_oauth:
            raise ValueError(
                "No authentication credentials supplied. "
                "Provide email+api_token or oauth_token."
            )

        self.subdomain = subdomain
        self.dry_run = dry_run
        self._allowed_host = f"{subdomain}.zendesk.com"
        self.rate_limiter = _RateLimiter()
        self._plan_prefetched = False

        self._default_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "zd-transfer/1.0",
        }
        self._basic_auth: Optional[tuple] = None
        self._bearer_token: Optional[str] = None

        # OAuth refresh support
        self._oauth_refresh_token: Optional[str] = None
        self._oauth_client_id: Optional[str] = None
        self._oauth_client_secret: Optional[str] = None
        self._env_path: Optional[str] = None
        self._refresh_lock = threading.Lock()
        self._post_refresh_callback = None

        if using_oauth:
            if not isinstance(oauth_token, str) or not oauth_token.strip():
                raise ValueError("oauth_token must be a non-empty string.")
            self._bearer_token = oauth_token
            self._auth_method = "oauth"
            if oauth_refresh_token and oauth_client_id and oauth_client_secret:
                self._oauth_refresh_token = oauth_refresh_token
                self._oauth_client_id = oauth_client_id
                self._oauth_client_secret = oauth_client_secret
                self._env_path = env_path
        else:
            email = self._sanitize_email(email)  # type: ignore[arg-type]
            if not api_token or not isinstance(api_token, str):
                raise ValueError("api_token must be a non-empty string.")
            self._basic_auth = (f"{email}/token", api_token)
            self._auth_method = "basic"

        self._token_refreshed = False
        self._thread_local = threading.local()
        self._base_url = self.BASE.format(subdomain=subdomain)

    def _get_session(self) -> requests.Session:
        """
        Return a `requests.Session` scoped to the current thread.
        Each thread gets its own connection pool so concurrent requests
        don't serialize on the urllib3 pool lock. Headers/auth are
        replicated from the stored credentials. The current bearer token
        is always re-applied so token refreshes propagate to all threads.
        """
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(self._default_headers)
            self._thread_local.session = sess
        if self._bearer_token:
            sess.headers["Authorization"] = f"Bearer {self._bearer_token}"
        elif self._basic_auth:
            sess.auth = self._basic_auth
        return sess

    # Backwards-compat: a few callers (phase3_content attachment upload) reach
    # into `client._session` directly to share auth. Expose the current
    # thread's session under the same name without breaking those call sites.
    @property
    def _session(self) -> requests.Session:
        return self._get_session()

    # ------------------------------------------------------------------ #
    #  Input sanitization                                                 #
    # ------------------------------------------------------------------ #

    @classmethod
    def _sanitize_subdomain(cls, subdomain: str) -> str:
        """
        Allow only alphanumeric characters and hyphens (Zendesk subdomain rules).
        Raises ValueError on anything that could be used for path traversal or
        host injection.
        """
        if not subdomain or not isinstance(subdomain, str):
            raise ValueError("Zendesk subdomain must be a non-empty string.")
        cleaned = subdomain.strip().lower()
        if not cls._SUBDOMAIN_RE.fullmatch(cleaned):
            raise ValueError(
                f"Invalid Zendesk subdomain '{cleaned}'. "
                "Must be 1-63 alphanumeric characters or hyphens, "
                "starting and ending with alphanumeric."
            )
        return cleaned

    @classmethod
    def _sanitize_email(cls, email: str) -> str:
        """Basic email format guard — not full RFC5322, just catches obvious injections."""
        if not email or not isinstance(email, str):
            raise ValueError("Email must be a non-empty string.")
        email = email.strip()
        if not cls._EMAIL_RE.fullmatch(email):
            raise ValueError(f"Invalid email format: '{email}'")
        return email

    # ------------------------------------------------------------------ #
    #  SSRF guard                                                         #
    # ------------------------------------------------------------------ #

    def _validate_url(self, url: str) -> str:
        """
        Ensure a URL belongs to the expected Zendesk subdomain.
        Prevents SSRF via a malicious 'next_page' value in paginated responses.
        Only the scheme and host are checked — query strings are not logged.
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            raise ZendeskAPIError(0, "Could not parse URL for SSRF validation.")

        if parsed.scheme != "https":
            raise ZendeskAPIError(0, f"Rejected non-HTTPS URL scheme: '{parsed.scheme}'")
        if parsed.netloc.lower() != self._allowed_host:
            raise ZendeskAPIError(
                0,
                f"SSRF guard: rejected URL for host '{parsed.netloc}' "
                f"(expected '{self._allowed_host}')"
            )
        return url

    # ------------------------------------------------------------------ #
    #  Core HTTP                                                           #
    # ------------------------------------------------------------------ #

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Execute a single HTTP request with retry/back-off on 429 and 5xx.
        On 401 with OAuth, attempts a one-shot token refresh and retries
        the request once with the new token.
        Raises ZendeskAPIError after exhausting retries.
        Raises ZendeskNetworkError on connectivity failures.
        Note: URL path (not query string) is included in error messages.
        """
        last_exc: Optional[Exception] = None
        # Extract path only for safe logging — never log query strings
        safe_url_label = urllib.parse.urlparse(url).path or url[:80]
        _token_refreshed = False  # only try refresh once per request
        self._token_refreshed = False  # shared cross-thread guard, reset per request

        for attempt in range(self.MAX_RETRIES):
            # Spend one token before sending. On a fresh client this blocks
            # at most ~300ms (default seed 200rpm = 1 token per 300ms); after
            # the first response the bucket retunes to the real account RPM.
            self.rate_limiter.acquire()
            try:
                resp = self._session.request(
                    method, url, timeout=self.REQUEST_TIMEOUT, **kwargs
                )
            except Timeout as exc:
                last_exc = exc
                wait = min(self.BACKOFF_BASE ** attempt, self.BACKOFF_CAP)
                time.sleep(wait)
                continue
            except ConnectionError as exc:
                last_exc = exc
                wait = min(self.BACKOFF_BASE ** attempt, self.BACKOFF_CAP)
                time.sleep(wait)
                continue
            except RequestException as exc:
                raise ZendeskNetworkError(
                    f"Unrecoverable network error on {method} {safe_url_label}: {exc}"
                ) from exc

            # Learn from every response so the bucket tracks the live account.
            try:
                self.rate_limiter.observe_response(resp.headers)
            except Exception:
                pass  # never fail a request because of telemetry

            # 429 and 503 both carry Retry-After per Zendesk docs.
            # 502/504 are transient infrastructure errors — exponential backoff.
            if resp.status_code in (429, 503):
                # Retry-After may be a float string like "1.5" — parse safely.
                # If Zendesk omits it (rare on 503), fall back to backoff.
                ra_header = resp.headers.get("Retry-After")
                try:
                    retry_after = float(ra_header) if ra_header else (
                        self.BACKOFF_BASE ** attempt
                    )
                except (TypeError, ValueError):
                    retry_after = float(self.BACKOFF_BASE ** attempt)
                # Floor at 1 s — Zendesk occasionally returns 0/0.1 right
                # at the limit boundary which causes immediate retry storms.
                retry_after = max(1.0, min(retry_after, self.BACKOFF_CAP))
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                wait = min(self.BACKOFF_BASE ** attempt, self.BACKOFF_CAP)
                time.sleep(wait)
                continue

            # 401 with OAuth — auto-refresh token once if we have the means
            if (
                resp.status_code == 401
                and self._auth_method == "oauth"
                and not _token_refreshed
                and self._oauth_refresh_token
                and not self.dry_run
            ):
                body = self._safe_decode(resp, 200)
                if "invalid_token" in body.lower():
                    with self._refresh_lock:
                        # Double-check: another thread may already have refreshed.
                        # Check the SHARED instance flag here (local _token_refreshed
                        # may be stale across threads).
                        if not self._token_refreshed:
                            if self._refresh_token():
                                self._token_refreshed = True
                                _token_refreshed = True
                                import logging as _logging
                                _logging.getLogger("src.client").info(
                                    "OAuth token refreshed automatically."
                                )
                                continue  # retry with new token
            return resp

        # All retries exhausted
        if last_exc:
            raise ZendeskNetworkError(
                f"Network error after {self.MAX_RETRIES} retries "
                f"on {method} {safe_url_label}: {type(last_exc).__name__}"
            ) from last_exc
        raise ZendeskAPIError(
            503,
            f"Server unavailable after {self.MAX_RETRIES} retries on {method}",
            url,
        )

    def _refresh_token(self) -> bool:
        """
        Attempt to refresh the OAuth bearer token using the stored
        refresh token and client credentials.

        On success, updates self._bearer_token and writes the new token
        to the .env file. Returns True if refreshed, False if refresh
        is not possible or failed.

        Thread-safe: callers must hold _refresh_lock.
        """
        if not self._oauth_refresh_token or not self._oauth_client_id:
            return False

        token_url = f"https://{self.subdomain}.zendesk.com/oauth/tokens"
        try:
            resp = requests.post(
                token_url,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._oauth_refresh_token,
                    "client_id": self._oauth_client_id,
                    "client_secret": self._oauth_client_secret,
                },
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return False

        if not resp.ok:
            return False

        data = resp.json()
        new_token = data.get("access_token")
        if not new_token:
            return False

        # Update in-memory token
        self._bearer_token = new_token

        # Update stored refresh token if Zendesk issued a new one
        new_refresh = data.get("refresh_token")
        if new_refresh:
            self._oauth_refresh_token = new_refresh

        # Persist new token to .env file
        if self._env_path:
            try:
                _update_env_token(self._env_path, new_token, new_refresh)
            except Exception:
                pass  # env write is best-effort; in-memory token is already updated

        # Notify the server-side ConnectionStore (if a callback was registered)
        if self._post_refresh_callback:
            try:
                self._post_refresh_callback(new_token, new_refresh)
            except Exception:
                pass

        return True

    def _url(self, path: str) -> str:
        """
        Resolve a path to a full URL.
        Full URLs (from pagination) are SSRF-validated before use.
        Relative paths are always anchored to our known base URL.
        """
        if path.startswith("http://") or path.startswith("https://"):
            return self._validate_url(path)
        # Relative path: strip leading slash and anchor to base
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _safe_decode(resp: requests.Response, max_bytes: int = 400) -> str:
        """
        Safely decode up to max_bytes of a response body.
        Uses errors='replace' so bad encodings never raise UnicodeDecodeError.
        Never touches resp.text (which triggers full decode).
        """
        return resp.content[:max_bytes].decode("utf-8", errors="replace")

    @staticmethod
    def _safe_json(resp: requests.Response) -> Dict:
        """
        Decode JSON from a response.
        Returns empty dict if body is empty.
        Raises ZendeskAPIError (not ValueError) on non-JSON bodies.
        """
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            # Non-JSON response (e.g. HTML error page from load balancer)
            preview = resp.content[:200].decode("utf-8", errors="replace")
            raise ZendeskAPIError(
                resp.status_code,
                f"Non-JSON response body: {preview}",
                resp.url,
            )

    # ------------------------------------------------------------------ #
    #  Public GET helpers                                                  #
    # ------------------------------------------------------------------ #

    def get(self, path: str, params: Optional[Dict] = None) -> Dict:
        """Single GET — returns parsed JSON body."""
        url = self._url(path)
        resp = self._request("GET", url, params=params)
        if not resp.ok:
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp), url
            )
        return self._safe_json(resp)

    def get_all(
        self, path: str, resource_key: str,
        params: Optional[Dict] = None,
    ) -> Generator[Dict, None, None]:
        """
        Paginated GET — yields every item across all pages.

        Supports both offset pagination (next_page URL) and
        cursor pagination (links.next URL).
        Each next_page URL is SSRF-validated before following.

        Optional query params (e.g. role[]=admin) can be passed
        to filter the initial request. Once cursor pagination kicks
        in, params are embedded in the next_page URL and query
        params are cleared.
        """
        url: Optional[str] = self._url(path)
        if params is None:
            params = {}
        params.setdefault("per_page", 100)
        seen_urls: set = set()  # infinite-loop guard

        while url:
            if url in seen_urls:
                raise ZendeskAPIError(0, "Pagination loop detected")
            seen_urls.add(url)

            resp = self._request("GET", url, params=params)
            if not resp.ok:
                raise ZendeskAPIError(
                    resp.status_code, self._safe_decode(resp), url
                )

            data = self._safe_json(resp)
            items = data.get(resource_key)
            if not isinstance(items, list):
                # Unexpected shape — stop pagination gracefully
                break
            yield from items

            # Determine next page URL — validate it belongs to our subdomain
            raw_next = (
                data.get("next_page")
                or data.get("links", {}).get("next")
            )
            if raw_next and isinstance(raw_next, str):
                try:
                    url = self._validate_url(raw_next)
                except ZendeskAPIError:
                    url = None  # reject suspicious next_page, stop pagination
            else:
                url = None
            params = {}  # already encoded in the full next_page URL

    # ------------------------------------------------------------------ #
    #  Public write helpers (honour dry_run)                              #
    # ------------------------------------------------------------------ #

    def post(self, path: str, payload: Dict) -> Dict:
        """Create a resource. No-ops in dry-run mode."""
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = self._url(path)
        resp = self._request("POST", url, json=payload)
        if not resp.ok:
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp, 500), url
            )
        return self._safe_json(resp)

    def put(self, path: str, payload: Dict) -> Dict:
        """Update a resource. No-ops in dry-run mode."""
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        url = self._url(path)
        resp = self._request("PUT", url, json=payload)
        if not resp.ok:
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp, 500), url
            )
        return self._safe_json(resp)

    def create_many(
        self,
        path: str,
        rkey_plural: str,
        items: List[Dict],
        *,
        poll_interval: float = 1.0,
        max_poll_seconds: int = 600,
    ) -> List[Dict]:
        """
        Bulk-create via Zendesk's `/<resource>/create_many` endpoint.

        path           — e.g. "organizations/create_many"
        rkey_plural    — the JSON wrapper key, e.g. "organizations"
        items          — list of payloads (max 100 per call per Zendesk docs)
        poll_interval  — seconds between job_status polls
        max_poll_seconds — give up after this many seconds total

        Returns the `results` array from the completed job, where each entry
        corresponds to the input item at the same `index`. Each result has
        either a new `id` (success) or an `error`/`details` field (failure).

        In dry-run mode returns a synthetic results list shaped like a
        successful batch so callers don't need to special-case.

        Raises ZendeskAPIError if the kickoff fails, the job ends in
        `failed` status, or polling times out.
        """
        if not items:
            return []
        if self.dry_run:
            return [{"index": i, "id": None, "dry_run": True}
                    for i, _ in enumerate(items)]

        url = self._url(path)
        resp = self._request("POST", url, json={rkey_plural: items})
        if not resp.ok:
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp, 500), url
            )
        data = self._safe_json(resp)
        job = data.get("job_status") or {}
        job_id = job.get("id")
        if not job_id:
            raise ZendeskAPIError(
                resp.status_code,
                f"create_many response had no job_status.id: keys={list(data.keys())}",
                url,
            )

        # Poll until terminal.
        deadline = time.monotonic() + max_poll_seconds
        while True:
            if time.monotonic() > deadline:
                raise ZendeskAPIError(
                    0, f"create_many job {job_id} timed out after "
                       f"{max_poll_seconds}s (last status={job.get('status')})",
                    url,
                )
            time.sleep(poll_interval)
            poll = self.get(f"job_statuses/{job_id}")
            job = poll.get("job_status") or {}
            status = job.get("status")
            if status == "completed":
                results = job.get("results") or []
                if not isinstance(results, list):
                    return []
                return results
            if status == "failed":
                raise ZendeskAPIError(
                    0, f"create_many job {job_id} failed: {job.get('message')}",
                    url,
                )
            # status is queued/working — keep polling

    def delete(self, path: str) -> None:
        """Delete a resource by path. No-ops in dry-run mode."""
        if self.dry_run:
            return
        url = self._url(path)
        resp = self._request("DELETE", url)
        if resp.status_code not in (200, 204):
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp), url
            )

    def destroy_many(
        self,
        path: str,
        ids: List,
        *,
        poll_interval: float = 1.0,
        max_poll_seconds: int = 600,
    ) -> List[Dict]:
        """
        Bulk-delete via Zendesk's `/<resource>/destroy_many?ids=...`
        endpoint. Currently supported by Zendesk only for a handful of
        resources (organizations, users, tickets). Each call is async and
        returns a job_status we poll to completion.

        path  — e.g. "organizations/destroy_many"
        ids   — list of resource IDs (max 100 per call per Zendesk docs).
                Callers should chunk larger lists themselves.

        Returns the `results` array from the completed job. Entries that
        succeeded carry the deleted id; entries that failed include an
        `error`/`details`/`status` field. In dry-run mode returns a
        synthetic results list of the same shape.

        Raises ZendeskAPIError if kickoff fails, the job ends in
        `failed` status, or polling times out.
        """
        if not ids:
            return []
        if self.dry_run:
            return [{"index": i, "id": rid, "dry_run": True}
                    for i, rid in enumerate(ids)]

        # Zendesk expects ?ids=1,2,3 — keep it under the 100-id cap.
        if len(ids) > 100:
            raise ValueError(
                f"destroy_many accepts at most 100 ids per call (got {len(ids)})"
            )
        id_csv = ",".join(str(i) for i in ids)
        url = self._url(path) + f"?ids={id_csv}"
        resp = self._request("DELETE", url)
        if resp.status_code not in (200, 202, 204):
            raise ZendeskAPIError(
                resp.status_code, self._safe_decode(resp, 500), url
            )
        data = self._safe_json(resp)
        job = data.get("job_status") or {}
        job_id = job.get("id")
        if not job_id:
            # Some Zendesk endpoints return 200/204 with no body for tiny
            # batches that completed synchronously. Treat that as success.
            return [{"index": i, "id": rid} for i, rid in enumerate(ids)]

        deadline = time.monotonic() + max_poll_seconds
        while True:
            if time.monotonic() > deadline:
                raise ZendeskAPIError(
                    0, f"destroy_many job {job_id} timed out after "
                       f"{max_poll_seconds}s (last status={job.get('status')})",
                    url,
                )
            time.sleep(poll_interval)
            poll = self.get(f"job_statuses/{job_id}")
            job = poll.get("job_status") or {}
            status = job.get("status")
            if status == "completed":
                results = job.get("results") or []
                return results if isinstance(results, list) else []
            if status == "failed":
                raise ZendeskAPIError(
                    0, f"destroy_many job {job_id} failed: {job.get('message')}",
                    url,
                )
            # queued/working — keep polling

    # ------------------------------------------------------------------ #
    #  Utility                                                            #
    # ------------------------------------------------------------------ #

    def ping(self) -> Dict:
        """
        Verify credentials and connectivity.
        Uses /users/me.json (the standard auth verification endpoint).
        Returns a dict with 'account' (name + subdomain) and 'user' keys
        for backward compatibility with callers that expect account info.

        Side effect: the response's X-Rate-Limit header is consumed by the
        rate limiter, so the first real workload request already knows the
        account's true RPM ceiling.
        """
        data = self.get("users/me.json")
        user = data.get("user", data)
        self._plan_prefetched = True
        return {
            "account": {
                "name": user.get("name", self.subdomain),
                "subdomain": self.subdomain,
            },
            "user": user,
        }

    def prefetch_plan(self) -> int:
        """
        Make one cheap GET so the rate limiter learns the account's real
        RPM ceiling before any bulk work starts. Returns the discovered RPM.

        Idempotent — subsequent calls are no-ops once the bucket has been
        calibrated. Safe to call from CLI entry points before kicking off
        a phase.

        If the prefetch fails (network blip, expired token), we log nothing
        but leave the conservative default in place; the first real request
        will recalibrate on success.
        """
        if self._plan_prefetched:
            return self.rate_limiter.rpm
        try:
            # users/me is the lightest authenticated endpoint and is what
            # ping() already uses. Going through self.get() ensures the
            # request flows through _request() → observe_response().
            self.get("users/me.json")
            self._plan_prefetched = True
        except (ZendeskAPIError, ZendeskNetworkError):
            # Leave conservative default; recalibration will happen later.
            pass
        return self.rate_limiter.rpm

    def list_resource(self, path: str, resource_key: str) -> List[Dict]:
        """Convenience: collect all paginated items into a list."""
        return list(self.get_all(path, resource_key))

    # ------------------------------------------------------------------ #
    #  Theme-specific helpers (async job-based workflow)                   #
    # ------------------------------------------------------------------ #

    def export_theme(
        self,
        theme_id: str,
        *,
        poll_interval: float = 2.0,
        max_poll_seconds: int = 120,
    ) -> bytes:
        """
        Export a help center theme as a ZIP file via the async job API.

        1. POST /guide/theming/jobs/themes/exports → creates export job
        2. Poll job until status="completed"
        3. Download ZIP from job.data.download.url

        Returns raw ZIP bytes.
        Raises ZendeskAPIError on failure or timeout.
        """
        if self.dry_run:
            return b""

        job_resp = self.post("guide/theming/jobs/themes/exports", {
            "job": {"attributes": {"theme_id": theme_id, "format": "zip"}}
        })
        job = job_resp.get("job") or {}
        job_id = job.get("id")
        if not job_id:
            raise ZendeskAPIError(
                0, "Theme export: response had no job.id",
                "guide/theming/jobs/themes/exports",
            )

        deadline = time.monotonic() + max_poll_seconds
        while True:
            if time.monotonic() > deadline:
                raise ZendeskAPIError(
                    0, f"Theme export job {job_id} timed out after {max_poll_seconds}s",
                    f"guide/theming/jobs/{job_id}",
                )
            time.sleep(poll_interval)
            poll = self.get(f"guide/theming/jobs/{job_id}")
            job = poll.get("job") or {}
            status = job.get("status")
            if status == "completed":
                download_url = job.get("data", {}).get("download", {}).get("url")
                if not download_url:
                    raise ZendeskAPIError(
                        0, "Theme export completed but no download.url in response",
                        f"guide/theming/jobs/{job_id}",
                    )
                dl = requests.get(download_url, timeout=self.REQUEST_TIMEOUT)
                dl.raise_for_status()
                return dl.content
            if status == "failed":
                err = job.get("errors") or job.get("data", {})
                raise ZendeskAPIError(
                    0, f"Theme export job {job_id} failed: {err}",
                    f"guide/theming/jobs/{job_id}",
                )

    def import_theme(
        self,
        brand_id: str,
        theme_zip: bytes,
        *,
        poll_interval: float = 2.0,
        max_poll_seconds: int = 120,
    ) -> str:
        """
        Import a help center theme from ZIP bytes via the async job API.

        1. POST /guide/theming/jobs/themes/imports → get upload URL + params
        2. Upload ZIP to the temporary upload URL
        3. Poll job until status="completed"
        4. Returns the new theme_id

        Raises ZendeskAPIError on failure or timeout.
        """
        if self.dry_run:
            return "dry_run_theme_id"

        job_resp = self.post("guide/theming/jobs/themes/imports", {
            "job": {"attributes": {"brand_id": brand_id, "format": "zip"}}
        })
        job = job_resp.get("job") or {}
        job_id = job.get("id")
        theme_id = job.get("data", {}).get("theme_id")
        upload_url = job.get("data", {}).get("upload", {}).get("url")
        upload_params = job.get("data", {}).get("upload", {}).get("parameters", {})
        if not job_id or not upload_url or not theme_id:
            raise ZendeskAPIError(
                0, "Theme import: response missing job.id, theme_id, or upload.url",
                "guide/theming/jobs/themes/imports",
            )

        files = {"file": ("theme.zip", io.BytesIO(theme_zip))}
        form_data = dict(upload_params) if upload_params else {}
        up = requests.post(
            upload_url, data=form_data, files=files,
            timeout=self.REQUEST_TIMEOUT,
        )
        if not up.ok:
            raise ZendeskAPIError(
                up.status_code,
                f"Theme ZIP upload failed: {up.content[:200].decode('utf-8', errors='replace')}",
                upload_url,
            )

        deadline = time.monotonic() + max_poll_seconds
        while True:
            if time.monotonic() > deadline:
                raise ZendeskAPIError(
                    0, f"Theme import job {job_id} timed out after {max_poll_seconds}s",
                    f"guide/theming/jobs/{job_id}",
                )
            time.sleep(poll_interval)
            poll = self.get(f"guide/theming/jobs/{job_id}")
            job = poll.get("job") or {}
            status = job.get("status")
            if status == "completed":
                return str(theme_id)
            if status == "failed":
                errs = job.get("errors") or job.get("data", {})
                raise ZendeskAPIError(
                    0, f"Theme import job {job_id} failed: {errs}",
                    f"guide/theming/jobs/{job_id}",
                )

    def publish_theme(self, theme_id: str) -> Dict:
        """Publish (make live) a help center theme."""
        return self.post(f"guide/theming/themes/{theme_id}/publish", {})

    def list_themes(self, brand_id: Optional[str] = None) -> List[Dict]:
        """List themes. Optionally filter by brand_id."""
        path = "guide/theming/themes"
        if brand_id:
            path += f"?brand_id={brand_id}"
        data = self.get(path)
        return data.get("themes", []) if isinstance(data, dict) else []

    def get_help_center_state(self, brand_id: Optional[str] = None) -> str:
        """
        Check if Help Center is enabled on the target.

        Returns 'enabled', 'disabled', 'restricted', or 'unknown'.
        If brand_id is given, checks that brand's help_center_state.
        Otherwise checks the first brand in the account.
        """
        try:
            brands = self.list_resource("brands", "brands")
        except (ZendeskAPIError, ZendeskNetworkError):
            return "unknown"
        target_brand = None
        if brand_id:
            for b in brands:
                if str(b.get("id")) == str(brand_id):
                    target_brand = b
                    break
        else:
            target_brand = brands[0] if brands else None
        if not target_brand:
            return "unknown"
        return target_brand.get("help_center_state", "unknown")

    def enable_help_center(self, brand_id: Optional[str] = None) -> bool:
        """
        Enable Help Center for a brand.
        If brand_id is None, enables on the first/default brand.
        Returns True if enabled, False if already enabled or on failure.
        """
        if self.dry_run:
            return True
        target_brand_id = brand_id
        if not target_brand_id:
            try:
                brands = self.list_resource("brands", "brands")
                if brands:
                    target_brand_id = str(brands[0].get("id"))
            except (ZendeskAPIError, ZendeskNetworkError):
                return False
        if not target_brand_id:
            return False
        try:
            self.put(f"brands/{target_brand_id}", {
                "brand": {"help_center_state": "enabled"}
            })
            return True
        except (ZendeskAPIError, ZendeskNetworkError):
            return False
