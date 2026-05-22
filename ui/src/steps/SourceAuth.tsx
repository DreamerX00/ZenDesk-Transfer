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
import type { Role } from "../types";
import { btn } from "./PreFlight";

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
  const [activeRole, setActiveRole] = useState<Role>("source");

  const roleTabs: Array<{
    role: Role;
    label: string;
    count: number;
    selected: boolean;
  }> = [
    {
      role: "source",
      label: "Source workspace",
      count: sourceConnections.length,
      selected: Boolean(sourceConnectionId),
    },
    {
      role: "target",
      label: "Target workspace",
      count: targetConnections.length,
      selected: Boolean(targetConnectionId),
    },
  ];

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--info">
        <strong>Secure connection profiles:</strong> each tenant can be linked
        with OAuth, a direct token, or a shared environment file. Credentials
        remain encrypted on the backend and are never echoed back to the UI.
      </div>

      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Workspace connections</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Switch between source and target with tabs instead of scrolling
              through both account sections.
            </p>
          </div>
        </div>

        <div className="zd-section-tabs" style={{ paddingLeft: 0, paddingRight: 0 }}>
          {roleTabs.map((tab) => (
            <button
              key={tab.role}
              className={`zd-section-tab${activeRole === tab.role ? " is-active" : ""}`}
              onClick={() => setActiveRole(tab.role)}
              type="button"
            >
              <strong>{tab.label}</strong>
              <span>
                {tab.count} saved
                {tab.selected ? " · selected" : ""}
              </span>
            </button>
          ))}
        </div>

        <div className="zd-tab-panel">
          {activeRole === "source" ? (
            <RoleSection
              role="source"
              connections={sourceConnections}
              selectedId={sourceConnectionId}
              onSelect={setSourceConnection}
              onRefresh={async () => setSourceConnections(await listConnections("source"))}
            />
          ) : null}

          {activeRole === "target" ? (
            <RoleSection
              role="target"
              connections={targetConnections}
              selectedId={targetConnectionId}
              onSelect={setTargetConnection}
              onRefresh={async () => setTargetConnections(await listConnections("target"))}
            />
          ) : null}
        </div>
      </div>

      <div className="zd-inline-actions">
        <button onClick={() => setStep("preflight")} style={btn("secondary")} type="button">
          Back
        </button>
        <button
          onClick={() => setStep("choose-phases")}
          disabled={!sourceConnectionId || !targetConnectionId}
          style={btn(!sourceConnectionId || !targetConnectionId ? "disabled" : "primary")}
          type="button"
        >
          Continue
        </button>
      </div>
    </div>
  );
}

function RoleSection({
  role,
  connections,
  selectedId,
  onSelect,
  onRefresh,
}: {
  role: Role;
  connections: ReturnType<typeof useStore.getState>["sourceConnections"];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onRefresh: () => Promise<void>;
}) {
  const label = role === "source" ? "Source" : "Target";

  return (
    <div className="zd-stack">
      <div className="zd-panel-header">
        <div>
          <h3>{label} workspace</h3>
          <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
            Pick an existing secure profile or create a new connection for the{" "}
            {label.toLowerCase()} Zendesk tenant.
          </p>
        </div>
        <div className="zd-chip zd-chip--ghost">
          <b>Profiles</b>
          <span>{connections.length}</span>
        </div>
      </div>

      {connections.length === 0 ? (
        <div className="zd-empty-state">
          <h3 style={{ marginTop: 0, fontSize: "1.1rem" }}>No saved {role} profiles</h3>
          <p style={{ marginBottom: 0 }}>
            Create a connection below to unlock this side of the migration.
          </p>
        </div>
      ) : (
        <div className="zd-table-wrap">
          <table className="zd-table">
            <thead>
              <tr>
                <th style={{ width: 52 }}>Use</th>
                <th>Workspace</th>
                <th>Auth</th>
                <th style={{ width: 220 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((connection) => (
                <ConnectionRow
                  key={connection.id}
                  role={role}
                  label={label}
                  connection={connection}
                  selected={selectedId === connection.id}
                  onSelect={() => onSelect(connection.id)}
                  onAfterDelete={async () => {
                    await onRefresh();
                    if (selectedId === connection.id) {
                      onSelect(null);
                    }
                  }}
                  onAfterRefresh={async () => {
                    await onRefresh();
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div>
        <ConnectForm role={role} onConnected={onRefresh} />
      </div>
    </div>
  );
}

function ConnectionRow({
  role,
  label,
  connection,
  selected,
  onSelect,
  onAfterDelete,
  onAfterRefresh,
}: {
  role: Role;
  label: string;
  connection: ReturnType<typeof useStore.getState>["sourceConnections"][number];
  selected: boolean;
  onSelect: () => void;
  onAfterDelete: () => Promise<void>;
  onAfterRefresh: () => Promise<void>;
}) {
  const notify = useToast();
  const [busy, setBusy] = useState<"none" | "refresh" | "remove">("none");

  // Last-4 of the bearer token — masked() returns "****XXXX" or null.
  // Show it next to the auth label so a refresh produces a visible
  // change without leaking the token itself.
  const tokenTail = connection.auth_kind === "oauth"
    ? connection.oauth_token
    : connection.api_token;

  async function handleRefresh() {
    setBusy("refresh");
    try {
      const fresh = await refreshConnection(connection.id);
      await onAfterRefresh();
      notify({
        tone: "success",
        title: "Token refreshed",
        message: `${connection.subdomain}.zendesk.com now uses a new OAuth access token (${fresh.oauth_token ?? "****"}).`,
      });
    } catch (error) {
      notify({
        tone: "danger",
        title: "Refresh failed",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy("none");
    }
  }

  async function handleRemove() {
    setBusy("remove");
    try {
      await deleteConnection(connection.id);
      await onAfterDelete();
      notify({
        tone: "warning",
        title: `${label} profile removed`,
        message: `${connection.subdomain}.zendesk.com was deleted from saved connections.`,
      });
    } catch (error) {
      notify({
        tone: "danger",
        title: "Unable to remove profile",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy("none");
    }
  }

  const canRefresh = connection.auth_kind === "oauth";

  return (
    <tr>
      <td>
        <input
          type="radio"
          name={`conn-${role}`}
          checked={selected}
          onChange={onSelect}
        />
      </td>
      <td>
        <strong>{connection.subdomain}.zendesk.com</strong>
        <div style={{ color: "#5d787f", marginTop: 4 }}>
          {connection.account_name || "Workspace"}
        </div>
      </td>
      <td>
        {connection.auth_kind === "oauth" ? "OAuth" : "API token"}
        {tokenTail ? (
          <div style={{ color: "#5d787f", marginTop: 4, fontFamily: "var(--font-mono)" }}>
            {tokenTail}
          </div>
        ) : null}
      </td>
      <td>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {canRefresh ? (
            <button
              onClick={handleRefresh}
              disabled={busy !== "none"}
              style={btn(busy !== "none" ? "disabled" : "secondary")}
              type="button"
              title="Mint a new access token using the stored refresh token."
            >
              {busy === "refresh" ? "Refreshing..." : "Refresh"}
            </button>
          ) : null}
          <button
            onClick={handleRemove}
            disabled={busy !== "none"}
            style={btn(busy !== "none" ? "disabled" : "ghost-danger")}
            type="button"
          >
            {busy === "remove" ? "Removing..." : "Remove"}
          </button>
        </div>
      </td>
    </tr>
  );
}

function ConnectForm({
  role,
  onConnected,
}: {
  role: Role;
  onConnected: () => Promise<void>;
}) {
  const [mode, setMode] = useState<ConnectMode>("oauth");

  return (
    <div className="zd-panel" style={{ marginTop: 0 }}>
      <div className="zd-panel-header">
        <div>
          <h3>Create a new {role} profile</h3>
          <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
            Choose the onboarding path that best matches how this tenant is
            currently managed.
          </p>
        </div>
      </div>

      <div className="zd-section-tabs" style={{ paddingLeft: 0, paddingRight: 0, marginBottom: 14 }}>
        <button
          onClick={() => setMode("oauth")}
          className={`zd-section-tab${mode === "oauth" ? " is-active" : ""}`}
          type="button"
        >
          <strong>OAuth</strong>
        </button>
        <button
          onClick={() => setMode("direct")}
          className={`zd-section-tab${mode === "direct" ? " is-active" : ""}`}
          type="button"
        >
          <strong>Direct token</strong>
        </button>
        <button
          onClick={() => setMode("upload-env")}
          className={`zd-section-tab${mode === "upload-env" ? " is-active" : ""}`}
          type="button"
        >
          <strong>Upload .env</strong>
        </button>
      </div>

      {mode === "oauth" ? <OAuthForm role={role} onConnected={onConnected} /> : null}
      {mode === "direct" ? <DirectForm role={role} onConnected={onConnected} /> : null}
      {mode === "upload-env" ? <UploadEnvForm role={role} onConnected={onConnected} /> : null}
    </div>
  );
}

function OAuthForm({
  role,
  onConnected,
}: {
  role: Role;
  onConnected: () => Promise<void>;
}) {
  const notify = useToast();
  const [step, setStep] = useState<OAuthStep>("form");
  const [subdomain, setSubdomain] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [authorizeUrl, setAuthorizeUrl] = useState("");
  const [redirectUrl, setRedirectUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Audio cue when an OAuth error surfaces — pairs with the inline callout.
  useEffect(() => { if (err) playError(); }, [err]);

  async function generateLink() {
    setErr(null);
    setBusy(true);
    try {
      const { authorize_url } = await oauthStart({
        role,
        subdomain: subdomain.trim().toLowerCase(),
        client_id: clientId.trim(),
        client_secret: clientSecret,
      });
      setAuthorizeUrl(authorize_url);
      setStep("link");
      notify({
        tone: "info",
        title: "OAuth link generated",
        message: "Open the Zendesk consent page and paste the redirect URL back here.",
      });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function exchangeCode() {
    setErr(null);
    setBusy(true);
    try {
      await exchangeOAuthRedirect(redirectUrl.trim());
      setStep("form");
      setSubdomain("");
      setClientId("");
      setClientSecret("");
      setRedirectUrl("");
      await onConnected();
      notify({
        tone: "success",
        title: "OAuth profile saved",
        message: `${role === "source" ? "Source" : "Target"} workspace credentials are now available in the selector.`,
      });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  if (step === "link") {
    return (
      <div className="zd-stack">
        <div className="zd-callout zd-callout--info">
          Open the generated Zendesk approval URL, finish consent, then paste
          the full redirect URL from the browser address bar back into this form.
        </div>

        <pre className="zd-pre-block" style={{ margin: 0 }}>
          {authorizeUrl}
        </pre>

        <div className="zd-inline-actions">
          <button
            onClick={() => window.open(authorizeUrl, "_blank")}
            style={btn("primary")}
            type="button"
          >
            Open in browser
          </button>
          <button
            onClick={() => {
              void (async () => {
                try {
                  await navigator.clipboard.writeText(authorizeUrl);
                  notify({
                    tone: "success",
                    title: "Link copied",
                    message: "The OAuth approval URL is ready to paste into a browser.",
                  });
                } catch {
                  const area = document.createElement("textarea");
                  area.value = authorizeUrl;
                  document.body.appendChild(area);
                  area.select();
                  document.execCommand("copy");
                  document.body.removeChild(area);
                  notify({
                    tone: "success",
                    title: "Link copied",
                    message: "The OAuth approval URL was copied with the fallback clipboard flow.",
                  });
                }
              })();
            }}
            style={btn("secondary")}
            type="button"
          >
            Copy link
          </button>
        </div>

        <label className="zd-field">
          <span>Redirect URL returned by Zendesk</span>
          <input
            className="zd-input"
            value={redirectUrl}
            onChange={(event) => setRedirectUrl(event.target.value)}
            placeholder="http://localhost/callback?code=...&state=..."
          />
        </label>

        {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

        <div className="zd-inline-actions">
          <button
            onClick={exchangeCode}
            disabled={busy || !redirectUrl.trim()}
            style={btn(busy || !redirectUrl.trim() ? "disabled" : "primary")}
            type="button"
          >
            {busy ? "Exchanging..." : "Exchange code"}
          </button>
          <button
            onClick={() => {
              setStep("form");
              setErr(null);
            }}
            style={btn("secondary")}
            type="button"
          >
            Back
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--info">
        Register an OAuth client in Zendesk Admin Center for this tenant, then
        use those credentials here to mint a reusable connection profile.
      </div>

      <label className="zd-field">
        <span>Subdomain</span>
        <input
          className="zd-input"
          value={subdomain}
          onChange={(event) => setSubdomain(event.target.value)}
          placeholder="e.g. dreamer-12487"
        />
      </label>

      <label className="zd-field">
        <span>OAuth client ID</span>
        <input
          className="zd-input"
          value={clientId}
          onChange={(event) => setClientId(event.target.value)}
        />
      </label>

      <label className="zd-field">
        <span>OAuth client secret</span>
        <input
          className="zd-input"
          type="password"
          value={clientSecret}
          onChange={(event) => setClientSecret(event.target.value)}
        />
      </label>

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        <button
          onClick={generateLink}
          disabled={busy || !subdomain || !clientId || !clientSecret}
          style={btn(busy || !subdomain || !clientId || !clientSecret ? "disabled" : "primary")}
          type="button"
        >
          {busy ? "Generating..." : "Generate OAuth link"}
        </button>
      </div>
    </div>
  );
}

function DirectForm({
  role,
  onConnected,
}: {
  role: Role;
  onConnected: () => Promise<void>;
}) {
  const notify = useToast();
  const [subdomain, setSubdomain] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Invalid token / subdomain → error chime.
  useEffect(() => { if (err) playError(); }, [err]);

  async function connect() {
    setErr(null);
    setBusy(true);
    try {
      await createDirectConnection(role, subdomain.trim().toLowerCase(), token.trim());
      setSubdomain("");
      setToken("");
      await onConnected();
      notify({
        tone: "success",
        title: "Direct token profile saved",
        message: `${subdomain.trim().toLowerCase()}.zendesk.com is now ready for selection.`,
      });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--info">
        Use this when you already have a valid OAuth or API token and want to
        skip the browser-based Zendesk consent flow entirely.
      </div>

      <label className="zd-field">
        <span>Subdomain</span>
        <input
          className="zd-input"
          value={subdomain}
          onChange={(event) => setSubdomain(event.target.value)}
          placeholder="e.g. dreamer-12487"
        />
      </label>

      <label className="zd-field">
        <span>API or OAuth token</span>
        <input
          className="zd-input"
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="Paste a token"
        />
      </label>

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        <button
          onClick={connect}
          disabled={busy || !subdomain || !token}
          style={btn(busy || !subdomain || !token ? "disabled" : "primary")}
          type="button"
        >
          {busy ? "Connecting..." : "Save profile"}
        </button>
      </div>
    </div>
  );
}

function UploadEnvForm({
  role,
  onConnected,
}: {
  role: Role;
  onConnected: () => Promise<void>;
}) {
  const notify = useToast();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Bad .env (missing keys, unreadable file) → error chime.
  useEffect(() => { if (err) playError(); }, [err]);

  async function handleFile(event: ChangeEvent<HTMLInputElement>) {
    setErr(null);
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }

    setBusy(true);
    try {
      const text = await file.text();
      const subdomain = parseEnv(text, "ZENDESK_SUBDOMAIN");
      const token = parseEnv(text, "ZENDESK_OAUTH_TOKEN");
      if (!subdomain) {
        setErr(".env file is missing ZENDESK_SUBDOMAIN");
        return;
      }
      if (!token) {
        setErr(".env file is missing ZENDESK_OAUTH_TOKEN");
        return;
      }

      await createDirectConnection(role, subdomain, token);
      await onConnected();
      notify({
        tone: "success",
        title: "Environment profile imported",
        message: `${subdomain}.zendesk.com was added from the uploaded file.`,
      });
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  }

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--info">
        Import a <code>.env</code> file that includes both{" "}
        <code>ZENDESK_SUBDOMAIN</code> and <code>ZENDESK_OAUTH_TOKEN</code>.
      </div>

      <input
        className="zd-file-input"
        type="file"
        accept=".env,.txt,text/plain"
        disabled={busy}
        onChange={handleFile}
      />

      {busy ? (
        <div className="zd-status-pill zd-status-pill--neutral">Processing file</div>
      ) : null}
      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}
    </div>
  );
}

function parseEnv(text: string, key: string): string | null {
  const matcher = new RegExp(`^\\s*${key}\\s*=\\s*["']?([^"'\\r\\n]+)["']?\\s*$`, "m");
  const match = matcher.exec(text);
  return match ? match[1].trim() : null;
}
