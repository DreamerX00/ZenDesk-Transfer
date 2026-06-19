import { useEffect, useState } from "react";
import type { ChangeEvent } from "react";
import {
  createDirectConnection,
  deleteConnection,
  exchangeOAuthRedirect,
  listConnections,
  oauthStart,
  refreshConnection,
} from "../api/backend";
import { playError } from "../sound";
import { useStore } from "../state/store";
import { useToast } from "../toasts";
import type { Role, MaskedConnection } from "../types";

type ConnectMode = "oauth" | "direct" | "upload-env";
type OAuthStep = "form" | "link";

export function SourceAuth() {
  const setStep = useStore((s) => s.setStep);
  const sourceConnections = useStore((s) => s.sourceConnections);
  const targetConnections = useStore((s) => s.targetConnections);
  const setSourceConnections = useStore((s) => s.setSourceConnections);
  const setTargetConnections = useStore((s) => s.setTargetConnections);
  const sourceConnectionId = useStore((s) => s.sourceConnectionId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);
  const setSourceConnection = useStore((s) => s.setSourceConnection);
  const setTargetConnection = useStore((s) => s.setTargetConnection);

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--info">
        <strong>Keep your credentials safe.</strong> All connection details are encrypted on the server.
        You need to connect both a source (the account to copy from) and a target (the account to copy to).
      </div>

      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <h3>Your workspaces</h3>
        </div>

        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
          gap: 16,
        }}>
          <WorkspaceCard
            role="source"
            label="Source workspace"
            description="The account you want to copy settings from"
            connections={sourceConnections}
            selectedId={sourceConnectionId}
            onSelect={setSourceConnection}
            onRefresh={async () => setSourceConnections(await listConnections("source"))}
          />
          <WorkspaceCard
            role="target"
            label="Target workspace"
            description="The account you want to copy settings to"
            connections={targetConnections}
            selectedId={targetConnectionId}
            onSelect={setTargetConnection}
            onRefresh={async () => setTargetConnections(await listConnections("target"))}
          />
        </div>
      </div>

      <div className="zd-inline-actions">
        <button onClick={() => setStep("preflight")} className="zd-button zd-button--secondary" type="button">
          Back
        </button>
        <button
          onClick={() => setStep("choose-phases")}
          disabled={!sourceConnectionId || !targetConnectionId}
          className="zd-button zd-button--primary"
          type="button"
        >
          {sourceConnectionId && targetConnectionId ? "Continue" : "Select both workspaces first"}
        </button>
      </div>
    </div>
  );
}

function WorkspaceCard({
  role,
  label,
  description,
  connections,
  selectedId,
  onSelect,
  onRefresh,
}: {
  role: Role;
  label: string;
  description: string;
  connections: MaskedConnection[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onRefresh: () => Promise<void>;
}) {
  const [showForm, setShowForm] = useState(false);

  return (
    <div className="zd-card" style={{
      border: selectedId && connections.find(c => c.id === selectedId)
        ? "2px solid var(--accent)" : undefined
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
        <div>
          <h3 style={{ fontSize: 15, margin: 0 }}>{label}</h3>
          <p style={{ margin: "4px 0 0", color: "var(--text-soft)", fontSize: 12, lineHeight: 1.5 }}>
            {description}
          </p>
        </div>
        {selectedId ? <span className="zd-chip zd-chip--brand">Connected</span> : null}
      </div>

      {connections.length === 0 ? (
        <div style={{
          padding: 16, textAlign: "center", color: "var(--text-faint)",
          border: "1px dashed var(--border)", borderRadius: "var(--radius-card)",
          fontSize: 13, marginBottom: 12,
        }}>
          No saved connections yet
        </div>
      ) : (
        <div style={{ display: "grid", gap: 8, marginBottom: 12 }}>
          {connections.map((connection) => (
            <SavedConnection
              key={connection.id}
              connection={connection}
              selected={selectedId === connection.id}
              onSelect={() => onSelect(connection.id)}
              onAfterDelete={async () => {
                await onRefresh();
                if (selectedId === connection.id) onSelect(null);
              }}
              onAfterRefresh={onRefresh}
            />
          ))}
        </div>
      )}

      {showForm ? (
        <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
          <ConnectForm role={role} onConnected={onRefresh} onClose={() => setShowForm(false)} />
        </div>
      ) : (
        <button
          className="zd-button zd-button--secondary"
          onClick={() => setShowForm(true)}
          type="button"
          style={{ width: "100%", justifyContent: "center" }}
        >
          + Add connection
        </button>
      )}
    </div>
  );
}

function SavedConnection({
  connection,
  selected,
  onSelect,
  onAfterDelete,
  onAfterRefresh,
}: {
  connection: MaskedConnection;
  selected: boolean;
  onSelect: () => void;
  onAfterDelete: () => Promise<void>;
  onAfterRefresh: () => Promise<void>;
}) {
  const notify = useToast();
  const [busy, setBusy] = useState<"none" | "refresh" | "remove">("none");
  const canRefresh = connection.auth_kind === "oauth";

  async function handleRefresh() {
    setBusy("refresh");
    try {
      await refreshConnection(connection.id);
      await onAfterRefresh();
      notify({ tone: "success", title: "Token refreshed", message: `${connection.subdomain}.zendesk.com updated.` });
    } catch (error) {
      notify({ tone: "danger", title: "Refresh failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy("none"); }
  }

  async function handleRemove() {
    setBusy("remove");
    try {
      await deleteConnection(connection.id);
      await onAfterDelete();
      notify({ tone: "warning", title: "Connection removed", message: `${connection.subdomain}.zendesk.com deleted.` });
    } catch (error) {
      notify({ tone: "danger", title: "Remove failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy("none"); }
  }

  return (
    <label className={`zd-choice-card${selected ? " is-active" : ""}`} style={{ cursor: "pointer", padding: "10px 14px" }}>
      <input type="radio" name={`conn-${connection.auth_kind}`} checked={selected} onChange={onSelect} style={{ marginTop: 2 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <strong style={{ fontSize: 13 }}>{connection.subdomain}.zendesk.com</strong>
        <p style={{ margin: "2px 0 0", fontSize: 11 }}>
          {connection.auth_kind === "oauth" ? "OAuth" : "API token"}
        </p>
      </div>
      <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
        {canRefresh ? (
          <button
            onClick={(e) => { e.preventDefault(); void handleRefresh(); }}
            disabled={busy !== "none"}
            className="zd-button zd-button--ghost"
            style={{ minHeight: 30, padding: "0 8px", fontSize: 11 }}
            type="button"
          >
            {busy === "refresh" ? "..." : "Refresh"}
          </button>
        ) : null}
        <button
          onClick={(e) => { e.preventDefault(); void handleRemove(); }}
          disabled={busy !== "none"}
          className="zd-button zd-button--ghost-danger"
          style={{ minHeight: 30, padding: "0 8px", fontSize: 11 }}
          type="button"
        >
          {busy === "remove" ? "..." : "Remove"}
        </button>
      </div>
    </label>
  );
}

function ConnectForm({
  role,
  onConnected,
  onClose,
}: {
  role: Role;
  onConnected: () => Promise<void>;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<ConnectMode>("oauth");

  return (
    <div className="zd-stack" style={{ gap: 10 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {(["oauth", "direct", "upload-env"] as const).map((m) => (
          <button
            key={m}
            className={`zd-button ${mode === m ? "zd-button--primary" : "zd-button--ghost"}`}
            onClick={() => setMode(m)}
            type="button"
            style={{ minHeight: 32, padding: "0 10px", fontSize: 12 }}
          >
            {m === "oauth" ? "OAuth" : m === "direct" ? "API token" : "Upload .env"}
          </button>
        ))}
      </div>

      {mode === "oauth" ? <OAuthForm role={role} onConnected={onConnected} onClose={onClose} /> : null}
      {mode === "direct" ? <DirectForm role={role} onConnected={onConnected} onClose={onClose} /> : null}
      {mode === "upload-env" ? <UploadEnvForm role={role} onConnected={onConnected} onClose={onClose} /> : null}
    </div>
  );
}

function OAuthForm({ role, onConnected, onClose }: { role: Role; onConnected: () => Promise<void>; onClose: () => void }) {
  const notify = useToast();
  const [step, setStep] = useState<OAuthStep>("form");
  const [subdomain, setSubdomain] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [authorizeUrl, setAuthorizeUrl] = useState("");
  const [redirectUrl, setRedirectUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (err) playError(); }, [err]);

  async function generateLink() {
    setErr(null); setBusy(true);
    try {
      const { authorize_url } = await oauthStart({
        role, subdomain: subdomain.trim().toLowerCase(), client_id: clientId.trim(), client_secret: clientSecret,
      });
      setAuthorizeUrl(authorize_url); setStep("link");
      notify({ tone: "info", title: "Link generated", message: "Open it in your browser and paste the redirect URL back here." });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); }
  }

  async function exchangeCode() {
    setErr(null); setBusy(true);
    try {
      await exchangeOAuthRedirect(redirectUrl.trim());
      setStep("form"); reset(); await onConnected(); onClose();
      notify({ tone: "success", title: "Connected!", message: "Workspace is ready to use." });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); }
  }

  function reset() {
    setSubdomain(""); setClientId(""); setClientSecret(""); setRedirectUrl("");
  }

  if (step === "link") {
    return (
      <div className="zd-stack" style={{ gap: 10 }}>
        <div className="zd-callout zd-callout--info" style={{ fontSize: 12 }}>
          Open the link below, authorize in Zendesk, then paste the redirect URL back here.
        </div>
        <pre className="zd-pre-block" style={{ margin: 0, fontSize: 12, maxHeight: 80 }}>{authorizeUrl}</pre>
        <div className="zd-inline-actions">
          <button onClick={() => window.open(authorizeUrl, "_blank")} className="zd-button zd-button--primary" type="button">Open link</button>
          <button onClick={async () => { await navigator.clipboard.writeText(authorizeUrl); notify({ tone: "success", title: "Copied!" }); }} className="zd-button zd-button--secondary" type="button">Copy</button>
        </div>
        <label className="zd-field">
          <span>Redirect URL</span>
          <input className="zd-input" value={redirectUrl} onChange={(e) => setRedirectUrl(e.target.value)} placeholder="http://localhost/callback?code=..." />
        </label>
        {err ? <div className="zd-callout zd-callout--danger" style={{ fontSize: 12 }}>{err}</div> : null}
        <div className="zd-inline-actions">
          <button onClick={exchangeCode} disabled={busy || !redirectUrl.trim()} className="zd-button zd-button--primary" type="button">
            {busy ? "Exchanging..." : "Complete connection"}
          </button>
          <button onClick={() => { setStep("form"); setErr(null); }} className="zd-button zd-button--ghost" type="button">Back</button>
        </div>
      </div>
    );
  }

  return (
    <div className="zd-stack" style={{ gap: 10 }}>
      <p style={{ margin: 0, fontSize: 12, color: "var(--text-soft)" }}>
        Register an OAuth app in Zendesk Admin Center, then enter the credentials here.
      </p>
      <label className="zd-field">
        <span>Subdomain</span>
        <input className="zd-input" value={subdomain} onChange={(e) => setSubdomain(e.target.value)} placeholder="e.g. your-company" />
      </label>
      <label className="zd-field">
        <span>Client ID</span>
        <input className="zd-input" value={clientId} onChange={(e) => setClientId(e.target.value)} />
      </label>
      <label className="zd-field">
        <span>Client secret</span>
        <input className="zd-input" type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} />
      </label>
      {err ? <div className="zd-callout zd-callout--danger" style={{ fontSize: 12 }}>{err}</div> : null}
      <button onClick={generateLink} disabled={busy || !subdomain || !clientId || !clientSecret} className="zd-button zd-button--primary" type="button" style={{ alignSelf: "flex-start" }}>
        {busy ? "Generating..." : "Generate OAuth link"}
      </button>
    </div>
  );
}

function DirectForm({ role, onConnected, onClose }: { role: Role; onConnected: () => Promise<void>; onClose: () => void }) {
  const notify = useToast();
  const [subdomain, setSubdomain] = useState("");
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (err) playError(); }, [err]);

  async function connect() {
    setErr(null); setBusy(true);
    try {
      await createDirectConnection(role, subdomain.trim().toLowerCase(), token.trim(), email.trim());
      setSubdomain(""); setEmail(""); setToken("");
      await onConnected(); onClose();
      notify({ tone: "success", title: "Connected!", message: `${subdomain.trim().toLowerCase()}.zendesk.com ready.` });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); }
  }

  return (
    <div className="zd-stack" style={{ gap: 10 }}>
      <p style={{ margin: 0, fontSize: 12, color: "var(--text-soft)" }}>
        Enter your Zendesk subdomain, admin email, and API token to connect directly.
      </p>
      <label className="zd-field">
        <span>Subdomain</span>
        <input className="zd-input" value={subdomain} onChange={(e) => setSubdomain(e.target.value)} placeholder="e.g. your-company" />
      </label>
      <label className="zd-field">
        <span>Admin email</span>
        <input className="zd-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="admin@example.com" />
      </label>
      <label className="zd-field">
        <span>API token</span>
        <input className="zd-input" type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Paste your token" />
      </label>
      {err ? <div className="zd-callout zd-callout--danger" style={{ fontSize: 12 }}>{err}</div> : null}
      <button onClick={connect} disabled={busy || !subdomain || !email || !token} className="zd-button zd-button--primary" type="button" style={{ alignSelf: "flex-start" }}>
        {busy ? "Connecting..." : "Save connection"}
      </button>
    </div>
  );
}

function UploadEnvForm({ role, onConnected, onClose }: { role: Role; onConnected: () => Promise<void>; onClose: () => void }) {
  const notify = useToast();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (err) playError(); }, [err]);

  async function handleFile(event: ChangeEvent<HTMLInputElement>) {
    setErr(null);
    const file = event.target.files?.[0];
    if (!file) return;

    setBusy(true);
    try {
      const text = await file.text();
      const subdomain = parseEnv(text, "ZENDESK_SUBDOMAIN");
      const token = parseEnv(text, "ZENDESK_OAUTH_TOKEN");
      if (!subdomain) { setErr("Missing ZENDESK_SUBDOMAIN in .env file"); return; }
      if (!token) { setErr("Missing ZENDESK_OAUTH_TOKEN in .env file"); return; }
      const email = parseEnv(text, "ZENDESK_EMAIL") || "";
      await createDirectConnection(role, subdomain, token, email);
      await onConnected(); onClose();
      notify({ tone: "success", title: "Imported!", message: `${subdomain}.zendesk.com ready.` });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally { setBusy(false); event.target.value = ""; }
  }

  return (
    <div className="zd-stack" style={{ gap: 10 }}>
      <p style={{ margin: 0, fontSize: 12, color: "var(--text-soft)" }}>
        Upload a <code>.env</code> file with <code>ZENDESK_SUBDOMAIN</code> and <code>ZENDESK_OAUTH_TOKEN</code>.
      </p>
      <input className="zd-file-input" type="file" accept=".env,.txt,text/plain" disabled={busy} onChange={handleFile} />
      {busy ? <span style={{ fontSize: 12, color: "var(--text-soft)" }}>Processing file...</span> : null}
      {err ? <div className="zd-callout zd-callout--danger" style={{ fontSize: 12 }}>{err}</div> : null}
    </div>
  );
}

function parseEnv(text: string, key: string): string | null {
  const matcher = new RegExp(`^\\s*${key}\\s*=\\s*["']?([^"'\\r\\n]+)["']?\\s*$`, "m");
  const match = matcher.exec(text);
  return match ? match[1].trim() : null;
}
