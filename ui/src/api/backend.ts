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
  email: string,
): Promise<{ connection_id: string; role: Role; subdomain: string }> {
  return await _fetch("/connections", {
    method: "POST",
    body: JSON.stringify({ role, subdomain, api_token: apiToken, email }),
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

export async function downloadFile(path: string, filename: string): Promise<void> {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const sep = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${_url(path)}${sep}download=1`, {
    headers: _bearer ? { Authorization: `Bearer ${_bearer}` } : {},
  });
  if (!resp.ok) {
    throw new BackendError(resp.status, await resp.text());
  }
  const blob = await resp.blob();
  triggerBlobDownload(filename, blob);
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
 * Uses fetch instead of EventSource so the bearer remains in the
 * Authorization header and never appears in the URL.
 */
export function openEventStream(
  migrationId: string,
  onMessage: (rec: unknown) => void,
  onDone: () => void,
  onError: (err: Event) => void,
): { close: () => void } {
  if (!_backendUrl) throw new Error("backend URL not configured");
  const controller = new AbortController();
  void readSseStream(
    _url(`/jobs/${encodeURIComponent(migrationId)}/events`),
    controller,
    onMessage,
    onDone,
    onError,
  );
  return { close: () => controller.abort() };
}

function triggerBlobDownload(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function readSseStream(
  url: string,
  controller: AbortController,
  onMessage: (rec: unknown) => void,
  onDone: () => void,
  onError: (err: Event) => void,
): Promise<void> {
  try {
    const resp = await fetch(url, {
      headers: _bearer ? { Authorization: `Bearer ${_bearer}` } : {},
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) {
      throw new BackendError(resp.status, await resp.text());
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx = buffer.indexOf("\n\n");
      while (idx >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleSseFrame(frame, onMessage, onDone);
        idx = buffer.indexOf("\n\n");
      }
    }
  } catch (err) {
    if (controller.signal.aborted) return;
    onError(new Event(err instanceof Error ? err.message : "sse-error"));
  }
}

function handleSseFrame(
  frame: string,
  onMessage: (rec: unknown) => void,
  onDone: () => void,
): void {
  const lines = frame.split(/\r?\n/);
  const event = lines
    .find((line) => line.startsWith("event:"))
    ?.slice("event:".length)
    .trim();
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice("data:".length).trimStart())
    .join("\n");
  if (event === "done") {
    onDone();
    return;
  }
  if (!data) return;
  try {
    onMessage(JSON.parse(data));
  } catch {
    onMessage(data);
  }
}
