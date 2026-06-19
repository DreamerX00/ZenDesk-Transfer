import { useState } from "react";
import { startMigration } from "../api/backend";
import { useStore } from "../state/store";

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
    if (!sourceConnectionId || !targetConnectionId) return;
    setErr(null); setBusy(true);
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

  const isDestructive = formatTarget && !dryRun;

  return (
    <div className="zd-stack">
      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Here's what you've set up</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Take a last look before we start the transfer.
            </p>
          </div>
          <span className={`zd-status-badge ${dryRun ? "zd-status-badge--neutral" : isDestructive ? "zd-status-badge--danger" : "zd-status-badge--success"}`}>
            {dryRun ? "Preview" : isDestructive ? "Will modify target" : "Live copy"}
          </span>
        </div>

        <dl className="zd-summary-grid">
          <div className="zd-summary-item">
            <dt>Copy from</dt>
            <dd>{sourceLabel}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Copy to</dt>
            <dd>{targetLabel}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>What to copy</dt>
            <dd>{selectedPhases.join(", ") || "Nothing selected"}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Max users</dt>
            <dd>{maxUsers ?? "All"}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Start from</dt>
            <dd>{usersFrom}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Clear target first</dt>
            <dd>{formatTarget ? "Yes" : "No"}</dd>
          </div>
        </dl>
      </div>

      {isDestructive ? (
        <div className="zd-callout zd-callout--danger">
          <strong>This will change your target account.</strong> Existing settings will be
          deleted before copying. A backup is saved automatically so you can undo later.
        </div>
      ) : null}

      {dryRun ? (
        <div className="zd-callout zd-callout--info">
          <strong>This is a preview run.</strong> No changes will be made to your target account.
          You'll see the full process without any risk.
        </div>
      ) : null}

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        <button
          onClick={() => setStep("choose-phases")}
          disabled={busy}
          className="zd-button zd-button--secondary"
          type="button"
        >
          Back
        </button>
        <button
          onClick={confirm}
          disabled={busy || !sourceConnectionId || !targetConnectionId}
          className={`zd-button ${busy || !sourceConnectionId || !targetConnectionId ? "" : isDestructive ? "zd-button--danger" : "zd-button--primary"}`}
          type="button"
        >
          {busy ? "Starting..." : dryRun ? "Start preview" : "Start transfer"}
        </button>
      </div>
    </div>
  );
}

function describeConnection(
  list: ReturnType<typeof useStore.getState>["sourceConnections"],
  id: string | null,
): string {
  if (!id) return "-";
  const match = list.find((connection) => connection.id === id);
  if (!match) return id;
  const authLabel = match.auth_kind === "oauth" ? "OAuth" : "API token";
  return `${match.subdomain}.zendesk.com · ${authLabel}`;
}
