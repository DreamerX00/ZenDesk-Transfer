import { useEffect, useMemo, useRef, useState } from "react";
import { cancelJob, getJobStatus, listBackups, startCleanup, startRestore, startRollback } from "../api/backend";
import { playError, playSuccess } from "../sound";
import { useStore } from "../state/store";
import { useToast } from "../toasts";
import type { LogRecord } from "../types";
import { computeEstimate, formatDuration } from "./progressEstimate";

const POLL_INTERVAL_MS = 2000;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STARTUP_PHASES = new Set(["", "starting", "idle"]);

const PHASE_NUM_TO_NAME: Record<number, string> = {
  1: "1-foundation",
  2: "2-business-logic",
  3: "3-content",
  4: "4-verify",
  5: "5-users",
};

export function ProgressDashboard() {
  const setStep = useStore((s) => s.setStep);
  const migrationId = useStore((s) => s.currentMigrationId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);
  const eventTail = useStore((s) => s.eventTail);
  const setEventTail = useStore((s) => s.setEventTail);
  const jobStatus = useStore((s) => s.jobStatus);
  const setJobStatus = useStore((s) => s.setJobStatus);
  const selectedPhases = useStore((s) => s.selectedPhases);

  const notify = useToast();
  const [err, setErr] = useState<string | null>(null);
  const stop = useRef(false);
  const lastPhase = useRef<string>("");
  const [now, setNow] = useState(Date.now());

  const selectedPhaseNames = useMemo(() => {
    const names = new Set<string>();
    for (const n of selectedPhases) {
      const name = PHASE_NUM_TO_NAME[n];
      if (name) names.add(name);
    }
    if (jobStatus.format_target === "True") names.add("format-target");
    names.add("extract");
    return names;
  }, [selectedPhases, jobStatus.format_target]);

  const estimate = useMemo(
    () => computeEstimate({
      status: jobStatus,
      events: eventTail,
      now,
      selectedPhases: selectedPhaseNames,
    }),
    [jobStatus, eventTail, now, selectedPhaseNames],
  );

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const phase = jobStatus.phase || "";
    if (phase === lastPhase.current) return;
    const prev = lastPhase.current;
    lastPhase.current = phase;
    if (!phase) return;
    if (phase === "failed" || phase === "cancelled") { playError(); return; }
    if (TERMINAL.has(phase)) return;
    if (STARTUP_PHASES.has(prev)) return;
    playSuccess();
  }, [jobStatus.phase]);

  useEffect(() => {
    if (!migrationId) return;
    stop.current = false;

    async function tick(): Promise<void> {
      if (stop.current || !migrationId) return;
      try {
        const response = await getJobStatus(migrationId, 100);
        setJobStatus(response.status);
        setEventTail(response.log_tail);
        const phase = response.status.phase || "";
        if (TERMINAL.has(phase)) {
          stop.current = true;
          if (phase === "completed") setTimeout(() => setStep("report"), 800);
          return;
        }
      } catch (error) {
        setErr(error instanceof Error ? error.message : String(error));
      }
      setTimeout(tick, POLL_INTERVAL_MS);
    }

    void tick();
    return () => { stop.current = true; };
  }, [migrationId, setEventTail, setJobStatus, setStep]);

  if (!migrationId) {
    return (
      <div className="zd-empty-state">
        <h3>No transfer is running</h3>
        <p>Start a transfer from the previous step to see live progress here.</p>
      </div>
    );
  }

  const phase = jobStatus.phase || "starting";
  const isTerminal = TERMINAL.has(phase);

  return (
    <div className="zd-stack">
      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Transfer progress</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              {isTerminal
                ? phase === "completed"
                  ? "Done! Your settings have been copied."
                  : "The transfer stopped before finishing."
                : "Copying your settings now..."}
            </p>
          </div>
          <span className={`zd-status-pill ${statusPillClass(phase)}`}>{phase}</span>
        </div>

        <div className="zd-summary-grid">
          <div className="zd-summary-item">
            <dt>Status</dt>
            <dd>{friendlyPhase(phase)}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Items processed</dt>
            <dd>{eventTail.length}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Time elapsed</dt>
            <dd>{formatDuration(estimate.totalElapsedSec)}</dd>
          </div>
          <div className="zd-summary-item" title={
            estimate.reason ??
            (estimate.itemsPerSec ? `Based on ${estimate.itemsPerSec.toFixed(2)} items/sec` : "")
          }>
            <dt>Estimated time left</dt>
            <dd>
              {isTerminal ? (
                phase === "completed" ? "Done" : "Stopped"
              ) : estimate.etaSec === null ? (
                <span style={{ color: "var(--text-faint)", fontStyle: "italic" }}>
                  {estimate.reason ?? "Estimating..."}
                </span>
              ) : (
                formatDuration(estimate.etaSec)
              )}
            </dd>
          </div>
        </div>

        {!isTerminal && estimate.etaSec !== null && estimate.phaseElapsedSec !== null ? (
          <div style={{ marginTop: 16 }}>
            <ProgressBar
              elapsed={estimate.totalElapsedSec ?? 0}
              remaining={estimate.etaSec}
              done={false}
            />
          </div>
        ) : isTerminal && phase === "completed" ? (
          <div style={{ marginTop: 16 }}>
            <ProgressBar elapsed={1} remaining={0} done={true} />
          </div>
        ) : null}
      </div>

      <Counters status={jobStatus} />
      <EventDisplay records={eventTail} />

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        {!isTerminal ? (
          <button
            onClick={() => {
              void cancelJob(migrationId)
                .then(() => notify({ tone: "warning", title: "Cancelling...", message: "Stopping the transfer." }))
                .catch(() => undefined);
            }}
            className="zd-button zd-button--danger"
            type="button"
          >
            Stop transfer
          </button>
        ) : (
          <DashboardActions
            targetConnectionId={targetConnectionId}
            setStep={setStep}
          />
        )}
      </div>
    </div>
  );
}

function Counters({ status }: { status: Record<string, string> }) {
  const counts: Array<[string, string]> = [];
  for (const key of Object.keys(status)) {
    if (key.startsWith("count:")) {
      counts.push([key.replace("count:", ""), status[key]]);
    }
  }

  if (counts.length === 0) return null;

  return (
    <div className="zd-card">
      <div className="zd-panel-header">
        <h3>Items copied so far</h3>
      </div>
      <div className="zd-summary-grid">
        {counts.map(([key, value]) => (
          <div key={key} className="zd-summary-item">
            <dt>{key}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </div>
    </div>
  );
}

function EventDisplay({ records }: { records: LogRecord[] }) {
  type ActionType = "CREATED" | "PURGED" | "SKIPPED" | "FAILED" | "MANUAL" | "NOTE";
  const actions: ActionType[] = ["CREATED", "FAILED", "SKIPPED", "PURGED", "MANUAL", "NOTE"];

  const grouped = actions.reduce((acc, action) => {
    acc[action] = records.filter((r) => r.action === action);
    return acc;
  }, {} as Record<ActionType, LogRecord[]>);

  const [activeTab, setActiveTab] = useState<ActionType>(() => {
    // Default to first non-empty tab, or CREATED.
    for (const action of actions) {
      if (grouped[action].length > 0) return action;
    }
    return "CREATED";
  });

  const [expanded, setExpanded] = useState(true);

  const visible = grouped[activeTab] ?? [];
  // Labels and colors for each tab
  const tabMeta: Record<ActionType, { label: string; color: string; bg: string }> = {
    CREATED: { label: "Copied", color: "#4caf50", bg: "rgba(76,175,80,0.1)" },
    FAILED: { label: "Failed", color: "#f44336", bg: "rgba(244,67,54,0.1)" },
    SKIPPED: { label: "Skipped", color: "#9e9e9e", bg: "rgba(158,158,158,0.1)" },
    PURGED: { label: "Removed", color: "#ff9800", bg: "rgba(255,152,0,0.1)" },
    MANUAL: { label: "Manual", color: "#9c27b0", bg: "rgba(156,39,176,0.1)" },
    NOTE: { label: "Notes", color: "#2196f3", bg: "rgba(33,150,243,0.1)" },
  };

  return (
    <div className="zd-log-frame">
      <div className="zd-log-header">
        <strong>Activity log</strong>
        <span>{records.length} total entries</span>
      </div>

      {/* Tabs by status */}
      <div className="zd-event-tabs">
        {actions.map((action) => (
          <button
            key={action}
            className={`zd-event-tab${activeTab === action ? " is-active" : ""}`}
            onClick={() => { setActiveTab(action); setExpanded(true); }}
            type="button"
            style={{
              "--tab-color": tabMeta[action].color,
              "--tab-bg": tabMeta[action].bg,
            } as React.CSSProperties}
          >
            {tabMeta[action].label}
            <span className="zd-event-tab-count">{grouped[action].length}</span>
          </button>
        ))}
      </div>

      {/* Expandable section */}
      <div className="zd-event-body">
        <button
          className="zd-event-toggle"
          onClick={() => setExpanded(!expanded)}
          type="button"
        >
          <span className={`zd-event-arrow${expanded ? " is-open" : ""}`}>&#9654;</span>
          {activeTab === "FAILED" ? "Errors" : tabMeta[activeTab].label} — {visible.length} entries
        </button>
        {expanded ? (
          <div className="zd-log-body">
            {visible.length === 0 ? (
              <div className="zd-empty-log">No entries for this status.</div>
            ) : (
              visible.map((record, index) => (
                <div key={index} style={{ color: colorFor(record.action) }}>
                  <span style={{ color: "rgba(255,255,255,0.4)" }}>
                    {(record.ts || "").slice(11, 19)}
                  </span>{" "}
                  {friendlyEvent(record)}
                </div>
              ))
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ProgressBar({ elapsed, remaining, done }: { elapsed: number; remaining: number; done: boolean }) {
  const total = elapsed + remaining;
  const pct = total > 0 ? Math.min(100, Math.max(0, (elapsed / total) * 100)) : 0;

  return (
    <div>
      <div className="zd-progress-track" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label="Transfer progress">
        <div className={`zd-progress-fill${done ? " is-done" : ""}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="zd-progress-labels">
        <span>{formatDuration(elapsed)} elapsed</span>
        <span>{done ? "Complete" : `${Math.round(pct)}%`}</span>
        <span>{done ? "" : `${formatDuration(remaining)} left`}</span>
      </div>
    </div>
  );
}

function DashboardActions({
  targetConnectionId,
  setStep,
}: {
  targetConnectionId: string | null;
  setStep: (s: import("../types").WizardStep) => void;
}) {
  const notify = useToast();
  const [op, setOp] = useState<"none" | "cleanup" | "rollback" | "restore">("none");
  const [phase, setPhase] = useState(1);
  const [backups, setBackups] = useState<import("../types").BackupInfo[]>([]);
  const [selectedBackup, setSelectedBackup] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function runCleanup() {
    if (!targetConnectionId) return;
    setBusy(true);
    try {
      const resp = await startCleanup(targetConnectionId);
      setStep("progress");
      notify({ tone: "info", title: "Cleanup started", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Cleanup failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  }

  async function runRollback() {
    if (!targetConnectionId) return;
    setBusy(true);
    try {
      const resp = await startRollback(targetConnectionId, phase);
      setStep("progress");
      notify({ tone: "info", title: "Rollback started", message: `Phase ${phase}, tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Rollback failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  }

  async function loadBackups() {
    try { setBackups(await listBackups()); }
    catch (error) { notify({ tone: "danger", title: "Cannot list backups", message: error instanceof Error ? error.message : String(error) }); }
  }

  useEffect(() => { if (op === "restore") void loadBackups(); }, [op]);

  async function runRestore() {
    if (!targetConnectionId || !selectedBackup) return;
    setBusy(true);
    try {
      const resp = await startRestore(targetConnectionId, selectedBackup);
      setStep("progress");
      notify({ tone: "info", title: "Restore started", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Restore failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%" }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button onClick={() => setStep("report")} className="zd-button zd-button--primary" type="button">View report</button>
        <button onClick={() => setOp(op === "cleanup" ? "none" : "cleanup")} className={`zd-button ${op === "cleanup" ? "zd-button--danger" : "zd-button--ghost"}`} type="button">Cleanup</button>
        <button onClick={() => setOp(op === "rollback" ? "none" : "rollback")} className={`zd-button ${op === "rollback" ? "zd-button--danger" : "zd-button--ghost"}`} type="button">Rollback</button>
        <button onClick={() => setOp(op === "restore" ? "none" : "restore")} className={`zd-button ${op === "restore" ? "zd-button--danger" : "zd-button--ghost"}`} type="button">Restore</button>
        <button onClick={() => setStep("preflight")} className="zd-button zd-button--ghost" type="button">New transfer</button>
      </div>

      {op === "cleanup" ? (
        <div className="zd-callout zd-callout--danger" style={{ margin: 0 }}>
          <strong>Delete everything this tool created.</strong>
          <p style={{ margin: "6px 0 12px" }}>Removes all settings that were copied. Uses the internal tracking to undo changes.</p>
          <button onClick={runCleanup} disabled={busy} className="zd-button zd-button--danger" type="button">
            {busy ? "Running..." : "Confirm cleanup"}
          </button>
        </div>
      ) : null}

      {op === "rollback" ? (
        <div className="zd-callout zd-callout--warning" style={{ margin: 0 }}>
          <strong>Undo one phase</strong>
          <p style={{ margin: "6px 0 12px" }}>Choose which part of the transfer to undo.</p>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <label className="zd-field" style={{ margin: 0, width: "auto" }}>
              <span style={{ fontSize: 12 }}>Phase:</span>
              <select className="zd-input" style={{ minHeight: 36, width: "auto" }} value={phase} onChange={(e) => setPhase(Number(e.target.value))}>
                <option value={1}>1 — Settings & Structure</option>
                <option value={2}>2 — Rules & Automation</option>
                <option value={3}>3 — Help Center</option>
              </select>
            </label>
            <button onClick={runRollback} disabled={busy} className="zd-button zd-button--danger" type="button">
              {busy ? "Running..." : "Undo phase"}
            </button>
          </div>
        </div>
      ) : null}

      {op === "restore" ? (
        <div className="zd-stack" style={{ margin: 0 }}>
          {backups.length === 0 ? (
            <div className="zd-callout zd-callout--info" style={{ margin: 0 }}>No backups found yet.</div>
          ) : (
            <>
              <div className="zd-choice-grid" style={{ gridTemplateColumns: "1fr" }}>
                {backups.map((b) => (
                  <label key={b.path} className={`zd-choice-card${selectedBackup === b.path ? " is-active" : ""}`}>
                    <input type="radio" name="dash-backup" checked={selectedBackup === b.path} onChange={() => setSelectedBackup(b.path)} />
                    <div>
                      <strong>{b.name}</strong>
                      <p>{b.resource_count != null ? `${b.resource_count} resources` : "Unknown size"}</p>
                    </div>
                  </label>
                ))}
              </div>
              <button onClick={runRestore} disabled={busy || !selectedBackup} className="zd-button zd-button--danger" type="button">
                {busy ? "Running..." : "Restore from backup"}
              </button>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

function statusPillClass(phase: string): string {
  if (phase === "completed") return "zd-status-pill--success";
  if (phase === "failed") return "zd-status-pill--danger";
  if (phase === "cancelled") return "zd-status-pill--warning";
  return "zd-status-pill--neutral";
}

function colorFor(action: LogRecord["action"]): string {
  switch (action) {
    case "CREATED": return "#84edc1";
    case "PURGED": return "#ffd982";
    case "SKIPPED": return "#98aeb3";
    case "FAILED": return "#ffb3c0";
    case "MANUAL": return "#b5a7ff";
    case "NOTE": return "#daf2ee";
    default: return "#daf2ee";
  }
}

function friendlyPhase(phase: string): string {
  const map: Record<string, string> = {
    "starting": "Starting up",
    "idle": "Waiting",
    "1-foundation": "Copying settings & structure",
    "2-business-logic": "Copying rules & automation",
    "3-content": "Copying Help Center",
    "4-verify": "Verifying everything matches",
    "5-users": "Copying users & agents",
    "completed": "Done!",
    "failed": "Something went wrong",
    "cancelled": "Stopped",
    "format-target": "Clearing target account",
    "extract": "Reading source account",
  };
  return map[phase] || phase;
}

function friendlyEvent(record: LogRecord): React.ReactNode {
  if (record.action === "NOTE") {
    return <span>{record.note || record.resource || ""}</span>;
  }
  const actionWord = record.action === "CREATED" ? "Copied" :
    record.action === "PURGED" ? "Removed" :
    record.action === "SKIPPED" ? "Skipped" :
    record.action === "FAILED" ? "Failed" :
    record.action === "MANUAL" ? "Manual" : record.action;

  return (
    <span>
      {actionWord} {record.resource || ""}
      {record.name ? ` — ${record.name}` : ""}
      {record.error ? ` (${record.error})` : ""}
    </span>
  );
}
