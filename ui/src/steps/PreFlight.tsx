import { useEffect, useState } from "react";
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
  const [showDetails, setShowDetails] = useState(false);

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
        <div className="zd-orbit-loader" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <h3>Checking your workspaces</h3>
        <p>
          Making sure both Zendesk accounts are reachable and ready for the transfer.
        </p>
      </div>
    );
  }

  if (err) {
    return (
      <div className="zd-callout zd-callout--danger">
        <strong>Something went wrong:</strong> {err}
      </div>
    );
  }

  const noSource = !result || !result.source.ok;
  const noTarget = !result || !result.target.ok;

  const sourceDetail = result?.source?.ok
    ? `${result.source.account_name || "Unknown"} (${result.source.subdomain}.zendesk.com)`
    : result?.source?.error || "Not connected yet";

  const targetDetail = result?.target?.ok
    ? `${result.target.account_name || "Unknown"} (${result.target.subdomain}.zendesk.com)`
    : result?.target?.error || "Not connected yet";

  return (
    <div className="zd-stack">
      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Workspace status</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              {noSource || noTarget
                ? "One or both workspaces need attention before you can start."
                : "Both workspaces are ready. You're good to go!"}
            </p>
          </div>
          <span className={`zd-status-badge ${noSource || noTarget ? "zd-status-badge--warning" : "zd-status-badge--success"}`}>
            {noSource || noTarget ? "Needs attention" : "All good"}
          </span>
        </div>

        <div className="zd-stack" style={{ gap: 10 }}>
          <div className="zd-connection-card">
            <div className={`zd-connection-icon ${result?.source?.ok ? "is-ok" : "is-error"}`}>
              {result?.source?.ok ? "✓" : "!"}
            </div>
            <div className="zd-connection-meta">
              <strong>Source — the account to copy from</strong>
              <span>{sourceDetail}</span>
            </div>
          </div>

          <div className="zd-connection-card">
            <div className={`zd-connection-icon ${result?.target?.ok ? "is-ok" : "is-error"}`}>
              {result?.target?.ok ? "✓" : "!"}
            </div>
            <div className="zd-connection-meta">
              <strong>Target — the account to copy to</strong>
              <span>{targetDetail}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Baseline details — collapsed by default */}
      {(result?.source_baseline || result?.baseline) ? (
        <>
          <button
            className="zd-button zd-button--ghost"
            onClick={() => setShowDetails(!showDetails)}
            type="button"
            style={{ alignSelf: "flex-start" }}
          >
            {showDetails ? "Hide" : "Show"} resource details
          </button>

          {showDetails ? (
            <>
              {result?.source_baseline && result.source_baseline.length > 0 ? (
                <div className="zd-card">
                  <div className="zd-panel-header">
                    <h3>What's in the source account</h3>
                    <span className="zd-chip zd-chip--ghost">
                      <b>Types</b>
                      <span>{result.source_baseline.length}</span>
                    </span>
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

              {result?.baseline && result.baseline.length > 0 ? (
                <div className="zd-card">
                  <div className="zd-panel-header">
                    <h3>What's in the target account</h3>
                    <span className="zd-chip zd-chip--ghost">
                      <b>Types</b>
                      <span>{result.baseline.length}</span>
                    </span>
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
            </>
          ) : null}
        </>
      ) : null}

      <div className="zd-inline-actions">
        <button
          onClick={() => setStep("source-auth")}
          className="zd-button zd-button--primary"
          type="button"
        >
          {noSource || noTarget ? "Connect workspaces" : "Manage connections"}
        </button>
        <button
          onClick={() => setStep("choose-phases")}
          disabled={noSource || noTarget}
          className="zd-button zd-button--secondary"
          type="button"
        >
          Continue
        </button>
      </div>
    </div>
  );
}
