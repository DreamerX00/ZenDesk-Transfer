import { useState } from "react";
import { startMigration } from "../api/backend";
import { useStore } from "../state/store";
import { btn } from "./PreFlight";

export function PreviewConfirm() {
  const setStep = useStore((s) => s.setStep);
  const sourceConnectionId = useStore((s) => s.sourceConnectionId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);
  const sourceConnections = useStore((s) => s.sourceConnections);
  const targetConnections = useStore((s) => s.targetConnections);
  const selectedPhases = useStore((s) => s.selectedPhases);
  const maxUsers = useStore((s) => s.maxUsers);
  const usersFrom = useStore((s) => s.usersFrom);
  const dryRun = useStore((s) => s.dryRun);
  const formatTarget = useStore((s) => s.formatTarget);
  const setCurrentMigrationId = useStore((s) => s.setCurrentMigrationId);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const sourceLabel = describeConnection(sourceConnections, sourceConnectionId);
  const targetLabel = describeConnection(targetConnections, targetConnectionId);

  async function confirm() {
    if (!sourceConnectionId || !targetConnectionId) {
      return;
    }

    setErr(null);
    setBusy(true);
    try {
      const response = await startMigration({
        source_connection_id: sourceConnectionId,
        target_connection_id: targetConnectionId,
        phases: selectedPhases,
        max_users: maxUsers,
        users_from: usersFrom,
        dry_run: dryRun,
        format_target: formatTarget,
      });
      setCurrentMigrationId(response.migration_id);
      setStep("progress");
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
      setBusy(false);
    }
  }

  return (
    <div className="zd-stack">
      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Launch summary</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              One final review pass before the migration job is enqueued on the
              backend.
            </p>
          </div>
          <div className={`zd-status-pill ${dryRun ? "zd-status-pill--neutral" : formatTarget ? "zd-status-pill--danger" : "zd-status-pill--success"}`}>
            {dryRun ? "Dry run" : formatTarget ? "Destructive write" : "Live write"}
          </div>
        </div>

        <dl className="zd-summary-grid">
          <div className="zd-summary-item">
            <dt>Source</dt>
            <dd>{sourceLabel}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Target</dt>
            <dd>{targetLabel}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Phases</dt>
            <dd>{selectedPhases.join(", ") || "None"}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Max users</dt>
            <dd>{maxUsers ?? "All"}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Offset</dt>
            <dd>{usersFrom}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Format target</dt>
            <dd>{formatTarget ? "Yes" : "No"}</dd>
          </div>
        </dl>
      </div>

      {formatTarget && !dryRun ? (
        <div className="zd-callout zd-callout--danger">
          <strong>Destructive run warning:</strong> formatting the target removes
          user-created configuration before recreation. A backup is taken by the
          backend, but this action is not reversible from the UI itself.
        </div>
      ) : null}

      {dryRun ? (
        <div className="zd-callout zd-callout--info">
          <strong>Safe preview:</strong> this run will execute the migration flow
          without writing data into the target Zendesk tenant.
        </div>
      ) : null}

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        <button
          onClick={() => setStep("choose-phases")}
          disabled={busy}
          style={btn("secondary")}
          type="button"
        >
          {"<- Back"}
        </button>
        <button
          onClick={confirm}
          disabled={busy || !sourceConnectionId || !targetConnectionId}
          style={btn(
            busy || !sourceConnectionId || !targetConnectionId
              ? "disabled"
              : dryRun
                ? "primary"
                : "danger",
          )}
          type="button"
        >
          {busy ? "Starting..." : dryRun ? "Start dry run ->" : "Start migration ->"}
        </button>
      </div>
    </div>
  );
}

function describeConnection(
  list: ReturnType<typeof useStore.getState>["sourceConnections"],
  id: string | null,
): string {
  if (!id) {
    return "-";
  }

  const match = list.find((connection) => connection.id === id);
  if (!match) {
    return id;
  }

  const authLabel = match.auth_kind === "oauth" ? "OAuth" : "API token";
  return `${match.subdomain}.zendesk.com · ${authLabel}`;
}
