/**
 * Fetch wrappers for the FastAPI backend. Every call carries the
 * iframe session bearer token; we centralise the auth + error
 * unwrapping here so individual components stay focused on UI.
 */

import type {
  BackupInfo,
  MaskedConnection,
  MigrateRequest,
  MigrateResponse,
  MigrationInfo,
  JobStatusResponse,
  PreflightResult,
  Role,
  SessionResponse,
} from "../types";

let _backendUrl: string | null = null;
let _bearer: string | null = null;

/** Set the backend base URL — called once during boot. */
export function setBackendUrl(url: string): void {
  _backendUrl = url.replace(/\/+$/, "");
}

/** Set the iframe session bearer token. Cleared on /session DELETE. */
export function setBearer(token: string | null): void {
  _bearer = token;
}

function _url(path: string): string {
  if (!_backendUrl) throw new Error("backend URL not configured");
  return `${_backendUrl}/api/v1${path}`;
}

async function _fetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init.headers as Record<string, string>) || {}),
  };
  if (_bearer) headers["Authorization"] = `Bearer ${_bearer}`;
  const resp = await fetch(_url(path), { ...init, headers });
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!resp.ok) {
    const err =
      (body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : body) || `HTTP ${resp.status}`;
    const message =
      typeof err === "object" && err && "error" in err
        ? String((err as { error: unknown }).error)
        : String(err);
    throw new BackendError(resp.status, message);
  }
  return body as T;
}

export class BackendError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "BackendError";
  }
}

// -- Session ----------------------------------------------------------

/**
 * Establish an iframe session.  `bodyText` is the JSON envelope
 * (ZAFClient.context() + ts), `signatureHex` is HMAC-SHA256 of
 * `bodyText` under the manifest's backend_secret parameter.
 */
export async function postSession(
  bodyText: string,
  signatureHex: string,
): Promise<SessionResponse> {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const resp = await fetch(_url("/session"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Signature": signatureHex,
    },
    body: bodyText,
  });
  const text = await resp.text();
  if (!resp.ok) {
    throw new BackendError(resp.status, text || `HTTP ${resp.status}`);
  }
  const out = JSON.parse(text) as SessionResponse;
  _bearer = out.token;
  return out;
}

/**
 * Mint a session via /api/v1/standalone/session — no HMAC. Only works
 * when the backend was started with ZDX_STANDALONE_MODE=1.
 */
export async function postStandaloneSession(): Promise<SessionResponse> {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const resp = await fetch(_url("/standalone/session"), { method: "POST" });
  const text = await resp.text();
  if (!resp.ok) {
    throw new BackendError(resp.status, text || `HTTP ${resp.status}`);
  }
  const out = JSON.parse(text) as SessionResponse;
  _bearer = out.token;
  return out;
}

// -- Connections -----------------------------------------------------

export async function listConnections(
  role?: "source" | "target",
): Promise<MaskedConnection[]> {
  const q = role ? `?role=${role}` : "";
  const r = await _fetch<{ connections: MaskedConnection[] }>(
    `/connections${q}`,
  );
  return r.connections;
}

export async function deleteConnection(id: string): Promise<void> {
  await _fetch<{ deleted: boolean }>(`/connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/**
 * Mint a new OAuth access token using the stored refresh_token.
 * Returns the refreshed masked connection so the caller can update
 * the row in place (showing the new last-4) without a full re-list.
 */
export async function refreshConnection(id: string): Promise<MaskedConnection> {
  const r = await _fetch<{ refreshed: boolean; connection: MaskedConnection }>(
    `/connections/${encodeURIComponent(id)}/refresh`,
    { method: "POST" },
  );
  return r.connection;
}

// -- Operations (cleanup / rollback / restore) -----------------------

export async function listBackups(): Promise<BackupInfo[]> {
  const r = await _fetch<{ backups: BackupInfo[] }>("/backups");
  return r.backups;
}

export async function startCleanup(targetConnectionId: string): Promise<MigrateResponse> {
  return _fetch<MigrateResponse>("/jobs/cleanup", {
    method: "POST",
    body: JSON.stringify({ target_connection_id: targetConnectionId }),
  });
}

export async function startRollback(targetConnectionId: string, phase: number): Promise<MigrateResponse> {
  return _fetch<MigrateResponse>("/jobs/rollback", {
    method: "POST",
    body: JSON.stringify({ target_connection_id: targetConnectionId, phase }),
  });
}

export async function startRestore(targetConnectionId: string, backupPath: string): Promise<MigrateResponse> {
  return _fetch<MigrateResponse>("/jobs/restore", {
    method: "POST",
    body: JSON.stringify({ target_connection_id: targetConnectionId, backup_path: backupPath }),
  });
}

// -- OAuth -----------------------------------------------------------

export interface OAuthStartParams {
  role: "source" | "target";
  subdomain: string;
  client_id: string;
  client_secret: string;
  scope?: string;
}

export async function oauthStart(
  params: OAuthStartParams,
): Promise<{ authorize_url: string; state: string }> {
  return await _fetch("/oauth/start", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

/** CLI-parity OAuth exchange: operator pastes the redirect URL they
 *  copied from the browser after authorizing at Zendesk. The backend
 *  extracts `code` and `state`, exchanges for a bearer token. */
export async function exchangeOAuthRedirect(
  redirectUrl: string,
): Promise<{ connection_id: string; role: Role; subdomain: string }> {
  return await _fetch("/oauth/exchange-redirect", {
    method: "POST",
    body: JSON.stringify({ redirect_url: redirectUrl }),
  });
}

// -- Direct connections (API token / .env) ---------------------------

/** Create a connection directly from an API token — no OAuth dance. */
export async function createDirectConnection(
  role: Role,
  subdomain: string,
  apiToken: string,
): Promise<{ connection_id: string; role: Role; subdomain: string }> {
  return await _fetch("/connections", {
    method: "POST",
    body: JSON.stringify({ role, subdomain, api_token: apiToken }),
  });
}

// -- Preflight + jobs ------------------------------------------------

export async function preflight(
  source_connection_id: string,
  target_connection_id: string,
): Promise<PreflightResult> {
  return await _fetch("/preflight", {
    method: "POST",
    body: JSON.stringify({ source_connection_id, target_connection_id }),
  });
}

export async function startMigration(
  req: MigrateRequest,
): Promise<MigrateResponse> {
  return await _fetch("/jobs/migrate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function getJobStatus(
  migrationId: string,
  tail = 20,
): Promise<JobStatusResponse> {
  return await _fetch(
    `/jobs/${encodeURIComponent(migrationId)}/status?tail=${tail}`,
  );
}

export async function cancelJob(migrationId: string): Promise<void> {
  await _fetch(`/jobs/${encodeURIComponent(migrationId)}/cancel`, {
    method: "POST",
  });
}

export async function listMigrations(): Promise<MigrationInfo[]> {
  const r = await _fetch<{ migrations: MigrationInfo[] }>("/migrations");
  return r.migrations;
}

export async function getReport(migrationId: string): Promise<string> {
  return await _fetchText(`/migrations/${encodeURIComponent(migrationId)}/report`);
}

/** Fetch the JSONL audit log as raw text. */
export async function getMigrationLog(migrationId: string): Promise<string> {
  return await _fetchText(`/migrations/${encodeURIComponent(migrationId)}/log`);
}

/** Fetch the id_map.json as raw text (UI parses if it needs to). */
export async function getIdMap(migrationId: string): Promise<string> {
  return await _fetchText(`/migrations/${encodeURIComponent(migrationId)}/id-map`);
}

/**
 * Build a download URL that includes the bearer token as a query-param.
 * Used by <a href> click-to-download — needed because <a> can't carry
 * an Authorization header. The server accepts either bearer source.
 */
export function downloadUrl(path: string): string {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const tokParam = _bearer ? `&t=${encodeURIComponent(_bearer)}` : "";
  const sep = path.includes("?") ? "&" : "?";
  return `${_url(path)}${sep}download=1${tokParam}`;
}

async function _fetchText(path: string): Promise<string> {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const resp = await fetch(_url(path), {
    headers: _bearer ? { Authorization: `Bearer ${_bearer}` } : {},
  });
  if (!resp.ok) {
    throw new BackendError(resp.status, await resp.text());
  }
  return await resp.text();
}

// -- SSE event stream ------------------------------------------------

/**
 * Open an SSE connection for live progress. Returns the EventSource
 * so the caller can close() it on unmount.
 *
 * NOTE: EventSource doesn't allow custom headers, so we pass the
 * session token as `?t=` and the server (or a thin proxy) reads it
 * out of the querystring. Today the FastAPI app reads from the
 * Authorization header; this means SSE must rely on a session cookie
 * instead. For Phase D we'll wire credentials: 'include' onto fetch
 * and the cookie path will Just Work. Until then, this helper is
 * used by Phase F tests with the polling fallback.
 */
export function openEventStream(
  migrationId: string,
  onMessage: (rec: unknown) => void,
  onDone: () => void,
  onError: (err: Event) => void,
): EventSource {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const url =
    _url(`/jobs/${encodeURIComponent(migrationId)}/events`) +
    (_bearer ? `?t=${encodeURIComponent(_bearer)}` : "");
  const es = new EventSource(url, { withCredentials: true });
  es.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      onMessage(e.data);
    }
  };
  es.addEventListener("done", () => {
    onDone();
    es.close();
  });
  es.onerror = onError;
  return es;
}
