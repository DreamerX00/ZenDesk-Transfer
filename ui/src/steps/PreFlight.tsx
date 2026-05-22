import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { listConnections, preflight } from "../api/backend";
import { playError, playSuccess } from "../sound";
import { useStore } from "../state/store";
import type { PreflightResult } from "../types";

export function PreFlight() {
  const setStep = useStore((s) => s.setStep);
  const setSourceConnections = useStore((s) => s.setSourceConnections);
  const setTargetConnections = useStore((s) => s.setTargetConnections);
  const sourceConnectionId = useStore((s) => s.sourceConnectionId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);
  const setSourceConnection = useStore((s) => s.setSourceConnection);
  const setTargetConnection = useStore((s) => s.setTargetConnection);

  const [loading, setLoading] = useState(true);
  const [result, setResult] = useState<PreflightResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [src, tgt] = await Promise.all([
          listConnections("source"),
          listConnections("target"),
        ]);
        setSourceConnections(src);
        setTargetConnections(tgt);

        if (src.length === 1 && !sourceConnectionId) {
          setSourceConnection(src[0].id);
        }
        if (tgt.length === 1 && !targetConnectionId) {
          setTargetConnection(tgt[0].id);
        }

        const sId = sourceConnectionId || (src[0]?.id ?? "");
        const tId = targetConnectionId || (tgt[0]?.id ?? "");
        if (sId && tId) {
          setResult(await preflight(sId, tId));
        }
      } catch (error) {
        setErr(error instanceof Error ? error.message : String(error));
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Chime on outcome — once per terminal pre-flight state.
  useEffect(() => {
    if (loading) return;
    if (err) {
      playError();
      return;
    }
    if (result) {
      const ok = result.source?.ok && result.target?.ok;
      if (ok) playSuccess();
      else playError();
    }
  }, [loading, err, result]);

  if (loading) {
    return (
      <div className="zd-empty-state">
        <div className="zd-status-pill zd-status-pill--neutral">Running checks</div>
        <h3>Inspecting both Zendesk workspaces</h3>
        <p>
          Pulling available connections, testing source and target health, and
          reading the target baseline before the migration flow continues.
        </p>
      </div>
    );
  }

  if (err) {
    return <ErrorPanel msg={err} />;
  }

  const noSource = !result || !result.source.ok;
  const noTarget = !result || !result.target.ok;

  return (
    <div className="zd-stack">
      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Connection readiness</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Validate both endpoints before a migration run starts. Healthy
              accounts here unlock the next stage automatically.
            </p>
          </div>
          <div className={`zd-status-pill ${noSource || noTarget ? "zd-status-pill--warning" : "zd-status-pill--success"}`}>
            {noSource || noTarget ? "Needs attention" : "Ready"}
          </div>
        </div>

        <div className="zd-stack">
          <ConnectionRow label="Source" info={result?.source} />
          <ConnectionRow label="Target" info={result?.target} />
        </div>
      </div>

      {result?.source_baseline && result.source_baseline.length > 0 ? (
        <div className="zd-panel">
          <div className="zd-panel-header">
            <div>
              <h3>Source baseline snapshot</h3>
              <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
                Resources that exist in the source account. These will be
                extracted and migrated to the target.
              </p>
            </div>
            <div className="zd-chip zd-chip--ghost">
              <b>Resource types</b>
              <span>{result.source_baseline.length}</span>
            </div>
          </div>

          <div className="zd-table-wrap">
            <table className="zd-table">
              <thead>
                <tr>
                  <th>Resource</th>
                  <th>Count</th>
                </tr>
              </thead>
              <tbody>
                {result.source_baseline.map((resource) => (
                  <tr key={resource.resource}>
                    <td>{resource.resource}</td>
                    <td>{resource.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {result?.source_baseline_error ? (
        <div className="zd-callout zd-callout--warning">
          <strong>Source baseline note:</strong> {result.source_baseline_error}
        </div>
      ) : null}

      {result?.baseline && result.baseline.length > 0 ? (
        <div className="zd-panel">
          <div className="zd-panel-header">
            <div>
              <h3>Target baseline snapshot</h3>
              <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
                Existing resources detected in the target account. If you later
                enable target formatting, these objects are purged before
                recreation.
              </p>
            </div>
            <div className="zd-chip zd-chip--ghost">
              <b>Resource types</b>
              <span>{result.baseline.length}</span>
            </div>
          </div>

          <div className="zd-table-wrap">
            <table className="zd-table">
              <thead>
                <tr>
                  <th>Resource</th>
                  <th>Count</th>
                </tr>
              </thead>
              <tbody>
                {result.baseline.map((resource) => (
                  <tr key={resource.resource}>
                    <td>{resource.resource}</td>
                    <td>{resource.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {result?.baseline_error ? (
        <div className="zd-callout zd-callout--warning">
          <strong>Target baseline note:</strong> {result.baseline_error}
        </div>
      ) : null}

      <div className="zd-inline-actions">
        <button
          onClick={() => setStep("source-auth")}
          style={btn("primary")}
          type="button"
        >
          {noSource ? "Connect source ->" : "Manage connections ->"}
        </button>
        <button
          onClick={() => setStep("choose-phases")}
          disabled={noSource || noTarget}
          style={btn(noSource || noTarget ? "disabled" : "secondary")}
          type="button"
        >
          {"Continue ->"}
        </button>
      </div>
    </div>
  );
}

function ConnectionRow({
  label,
  info,
}: {
  label: string;
  info?: PreflightResult["source"];
}) {
  const ok = info?.ok ?? false;

  return (
    <div className="zd-connection-row">
      <span className={`zd-connection-dot ${ok ? "is-ok" : "is-error"}`} />
      <div className="zd-connection-meta">
        <strong>{label}</strong>
        <span>
          {ok
            ? `${info?.account_name || "Unknown account"} (${info?.subdomain || "?"}.zendesk.com)`
            : info?.error || "Connection is not configured yet"}
        </span>
      </div>
    </div>
  );
}

function ErrorPanel({ msg }: { msg: string }) {
  return (
    <div className="zd-callout zd-callout--danger">
      <strong>Pre-flight failed:</strong> {msg}
    </div>
  );
}

export function btn(
  kind:
    | "primary"
    | "secondary"
    | "danger"
    | "disabled"
    | "ghost"
    | "ghost-danger",
): CSSProperties {
  const base: CSSProperties = {
    minHeight: 44,
    padding: "0 18px",
    borderRadius: 14,
    border: "1px solid transparent",
    fontSize: 14,
    fontWeight: 700,
    letterSpacing: "-0.01em",
    cursor: "pointer",
    boxShadow: "0 14px 28px rgba(18, 53, 59, 0.08)",
  };

  if (kind === "primary") {
    return {
      ...base,
      color: "#ffffff",
      background: "linear-gradient(135deg, #0f9b76, #17494d)",
      boxShadow: "0 18px 34px rgba(15, 155, 118, 0.22)",
    };
  }

  if (kind === "danger") {
    return {
      ...base,
      color: "#ffffff",
      background: "linear-gradient(135deg, #d54d68, #b53049)",
      boxShadow: "0 18px 34px rgba(203, 61, 87, 0.22)",
    };
  }

  if (kind === "disabled") {
    return {
      ...base,
      color: "#7e9498",
      background: "rgba(215, 226, 223, 0.9)",
      borderColor: "rgba(18, 53, 59, 0.08)",
      cursor: "not-allowed",
      boxShadow: "none",
      opacity: 0.8,
    };
  }

  if (kind === "ghost") {
    return {
      ...base,
      color: "#2a4a51",
      background: "transparent",
      borderColor: "rgba(18, 53, 59, 0.1)",
      boxShadow: "none",
    };
  }

  if (kind === "ghost-danger") {
    return {
      ...base,
      color: "#b53049",
      background: "transparent",
      borderColor: "rgba(203, 61, 87, 0.12)",
      boxShadow: "none",
    };
  }

  return {
    ...base,
    color: "#143a41",
    background: "rgba(255, 255, 255, 0.88)",
    borderColor: "rgba(18, 53, 59, 0.1)",
  };
}
