import { useEffect, useRef, useState } from "react";
import { cancelJob, getJobStatus, listBackups, startCleanup, startRestore, startRollback } from "../api/backend";
import { playError, playSuccess } from "../sound";
import { useStore } from "../state/store";
import { useToast } from "../toasts";
import type { LogRecord } from "../types";
import { btn } from "./PreFlight";

const POLL_INTERVAL_MS = 2000;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STARTUP_PHASES = new Set(["", "starting", "idle"]);

// Estimated time per resource item (seconds), calibrated from observed
// Zendesk API latency. These account for: HTTP round-trip, Zendesk
// server processing, rate-limit pacing (the token-bucket throttle),
// and payload serialisation.  A single create takes ~0.6-1.2 s in
// practice; we use conservative upper-bound estimates so the bar
// doesn't prematurely show "overdue".
const RESOURCE_COST_SEC: Record<string, number> = {
  "groups":               0.8,
  "brands":               1.8,  // brand_url creation is slow
  "ticket_fields":        2.0,  // complex payload, multiple sub-calls
  "user_fields":          1.0,
  "organization_fields":  1.0,
  "custom_roles":         1.0,
  "ticket_forms":         1.5,
  "organizations":        1.0,
  "views":                1.2,
  "triggers":             1.0,
  "automations":          1.0,
  "macros":               1.0,
  "sla_policies":         1.2,
  "group_sla_policies":   1.2,
  "schedules":            1.0,
  "routing_attributes":   1.0,
  "dynamic_content_items": 1.0,
  "webhooks":             2.0,  // ZIS-backed, higher latency
  "categories":           1.5,  // HC parent object
  "sections":             1.0,
  "articles":             3.0,  // large payload, translation expansion
  "user_segments":        0.8,
  "users":                0.5,
};

// Which resource keys belong to each phase.
const PHASE_RESOURCES: Record<string, string[]> = {
  "extract":              ["groups", "brands", "ticket_fields", "user_fields", "organization_fields", "custom_roles", "ticket_forms", "organizations", "views", "triggers", "automations", "macros", "sla_policies", "group_sla_policies", "schedules", "routing_attributes", "dynamic_content_items", "webhooks", "categories", "sections", "articles", "user_segments", "users"],
  "format-target":        [],
  "1-foundation":         ["groups", "brands", "ticket_fields", "user_fields", "organization_fields", "custom_roles", "ticket_forms", "organizations"],
  "2-business-logic":     ["views", "triggers", "automations", "macros", "sla_policies", "group_sla_policies", "schedules", "routing_attributes", "dynamic_content_items", "webhooks"],
  "3-content":            ["categories", "sections", "articles", "user_segments"],
  "4-verify":             [],
  "5-users":              ["users"],
};

function fmtDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Estimate how many seconds a phase will take based on extracted resource counts. */
function estimatePhase(phase: string, status: Record<string, string>): number {
  const perPhase = PHASE_RESOURCES[phase];
  if (!perPhase || perPhase.length === 0) return 30; // minimal overhead
  let total = 5;  // base overhead per phase
  for (const rkey of perPhase) {
    const raw = status[`extracted_${rkey}`];
    const count = raw ? parseInt(raw, 10) : 0;
    if (count > 0) {
      total += count * (RESOURCE_COST_SEC[rkey] ?? 1.0);
    }
  }
  return Math.max(total, 10);
}

export function ProgressDashboard() {
  const setStep = useStore((s) => s.setStep);
  const migrationId = useStore((s) => s.currentMigrationId);
  const eventTail = useStore((s) => s.eventTail);
  const setEventTail = useStore((s) => s.setEventTail);
  const jobStatus = useStore((s) => s.jobStatus);
  const setJobStatus = useStore((s) => s.setJobStatus);

  const notify = useToast();
  const [err, setErr] = useState<string | null>(null);
  const stop = useRef(false);
  const lastPhase = useRef<string>("");
  const [now, setNow] = useState(Date.now());

  // Live clock tick — re-renders every second so the elapsed timer
  // advances smoothly.
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  // Each non-terminal phase transition means the previous phase
  // finished cleanly → success.mp3. Terminal "failed"/"cancelled" plays
  // the error chime; "completed" is celebrated by the toast layer.
  useEffect(() => {
    const phase = jobStatus.phase || "";
    if (phase === lastPhase.current) return;
    const prev = lastPhase.current;
    lastPhase.current = phase;
    if (!phase) return;
    if (phase === "failed" || phase === "cancelled") {
      playError();
      return;
    }
    if (TERMINAL.has(phase)) return; // "completed" handled elsewhere
    if (STARTUP_PHASES.has(prev)) return; // first real phase, nothing finished yet
    playSuccess();
  }, [jobStatus.phase]);

  useEffect(() => {
    if (!migrationId) {
      return;
    }
    stop.current = false;

    async function tick(): Promise<void> {
      if (stop.current || !migrationId) {
        return;
      }
      try {
        const response = await getJobStatus(migrationId, 100);
        setJobStatus(response.status);
        setEventTail(response.log_tail);
        const phase = response.status.phase || "";
        if (TERMINAL.has(phase)) {
          stop.current = true;
          if (phase === "completed") {
            setTimeout(() => setStep("report"), 800);
          }
          return;
        }
      } catch (error) {
        setErr(error instanceof Error ? error.message : String(error));
      }
      setTimeout(tick, POLL_INTERVAL_MS);
    }

    void tick();
    return () => {
      stop.current = true;
    };
  }, [migrationId, setEventTail, setJobStatus, setStep]);

  if (!migrationId) {
    return (
      <div className="zd-empty-state">
        <h3 style={{ marginTop: 0 }}>No migration is active</h3>
        <p style={{ marginBottom: 0 }}>
          Start a migration from the previous step to unlock live telemetry.
        </p>
      </div>
    );
  }

  const phase = jobStatus.phase || "starting";
  const isTerminal = TERMINAL.has(phase);

  return (
    <div className="zd-stack">
      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Run telemetry</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Live counters and event output from the backend migration worker.
            </p>
          </div>
          <div className={`zd-status-pill ${statusPillClass(phase)}`}>{phase}</div>
        </div>

        <div className="zd-summary-grid">
          <div className="zd-summary-item">
            <dt>Migration ID</dt>
            <dd>{migrationId}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Phase</dt>
            <dd>{phase}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Events captured</dt>
            <dd>{eventTail.length}</dd>
          </div>
          <div className="zd-summary-item">
            <dt>Duration</dt>
            <dd>
              <PhaseTimer
                phase={phase}
                startedAt={jobStatus.phase_started_at ?? null}
                now={now}
                isTerminal={isTerminal}
                status={jobStatus}
              />
            </dd>
          </div>
        </div>
      </div>

      <Counters status={jobStatus} />
      <EventTail records={eventTail} />

      {err ? <div className="zd-callout zd-callout--danger">{err}</div> : null}

      <div className="zd-inline-actions">
        {!isTerminal ? (
          <button
            onClick={() => {
              void cancelJob(migrationId)
                .then(() => {
                  notify({
                    tone: "warning",
                    title: "Cancellation requested",
                    message: "The backend has been asked to stop the current migration run.",
                  });
                })
                .catch(() => undefined);
            }}
            style={btn("danger")}
            type="button"
          >
            Cancel run
          </button>
        ) : null}

        {isTerminal ? (
          <DashboardOperations
            targetConnectionId={useStore((s) => s.targetConnectionId)}
            setStep={setStep}
          />
        ) : null}
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

  if (counts.length === 0) {
    return null;
  }

  return (
    <div className="zd-panel">
      <div className="zd-panel-header">
        <div>
          <h3>Resource counters</h3>
          <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
            Running totals reported by the backend for the active migration.
          </p>
        </div>
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

function EventTail({ records }: { records: LogRecord[] }) {
  return (
    <div className="zd-log-frame">
      <div className="zd-log-header">
        <strong>Event stream</strong>
        <span>Last {records.length || 0} migration log entries</span>
      </div>
      <div className="zd-log-body">
        {records.length === 0 ? (
          <div className="zd-empty-log">(no events yet)</div>
        ) : (
          records.map((record, index) => (
            <div key={index} style={{ color: colorFor(record.action) }}>
              <span style={{ color: "rgba(121, 166, 157, 0.82)" }}>
                {(record.ts || "").slice(11, 19)}
              </span>{" "}
              {record.action === "NOTE" ? (
                <span>{record.note || record.resource || ""}</span>
              ) : (
                <>
                  {record.action.padEnd(8, " ")} {record.resource || ""}{" "}
                  {record.name || record.source_id ? `| ${record.name || record.source_id}` : ""}
                  {record.error ? ` | ${record.error}` : ""}
                  {record.reason ? ` (${record.reason})` : ""}
                </>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function PhaseTimer({
  phase,
  startedAt,
  now,
  isTerminal,
  status,
}: {
  phase: string;
  startedAt: string | null;
  now: number;
  isTerminal: boolean;
  status: Record<string, string>;
}) {
  const estimate = estimatePhase(phase, status);

  if (!startedAt) {
    return <span>— / {fmtDuration(estimate)}</span>;
  }

  const elapsedMs = now - new Date(startedAt).getTime();
  const elapsedSec = Math.max(0, elapsedMs / 1000);
  const pct = Math.min(100, Math.round((elapsedSec / estimate) * 100));

  let barColor = "#84edc1";
  if (pct > 100) barColor = "#ffb3c0";
  else if (pct > 80) barColor = "#ffd982";

  return (
    <span title={`Elapsed: ${fmtDuration(elapsedSec)}  |  Estimated: ${fmtDuration(estimate)}`}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span>{fmtDuration(elapsedSec)} / {fmtDuration(estimate)}</span>
      {!isTerminal ? (
        <span style={{
          display: "inline-block", width: 48, height: 6, borderRadius: 3,
          background: "rgba(215, 226, 223, 0.4)", overflow: "hidden",
        }}>
          <span style={{
            display: "block", height: "100%", width: `${Math.min(pct, 100)}%`,
            background: barColor, borderRadius: 3,
            transition: "width 1s ease, background 0.5s ease",
          }} />
        </span>
      ) : null}
    </span>
  );
}

function DashboardOperations({
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
      notify({ tone: "info", title: "Cleanup launched", message: `Tracking run ${resp.migration_id}.` });
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
      notify({ tone: "info", title: "Rollback launched", message: `Phase ${phase}, tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Rollback failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  }

  async function loadBackups() {
    try {
      setBackups(await listBackups());
    } catch (error) {
      notify({ tone: "danger", title: "Cannot list backups", message: error instanceof Error ? error.message : String(error) });
    }
  }

  useEffect(() => { if (op === "restore") void loadBackups(); }, [op]);

  async function runRestore() {
    if (!targetConnectionId || !selectedBackup) return;
    setBusy(true);
    try {
      const resp = await startRestore(targetConnectionId, selectedBackup);
      setStep("progress");
      notify({ tone: "info", title: "Restore launched", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Restore failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%" }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button onClick={() => setStep("report")} style={btn("primary")} type="button">View report</button>
        <button onClick={() => setOp(op === "cleanup" ? "none" : "cleanup")} style={btn(op === "cleanup" ? "danger" : "ghost")} type="button">Cleanup</button>
        <button onClick={() => setOp(op === "rollback" ? "none" : "rollback")} style={btn(op === "rollback" ? "danger" : "ghost")} type="button">Rollback</button>
        <button onClick={() => setOp(op === "restore" ? "none" : "restore")} style={btn(op === "restore" ? "danger" : "ghost")} type="button">Restore</button>
        <button onClick={() => setStep("preflight")} style={btn("ghost")} type="button">New migration</button>
      </div>

      {op === "cleanup" ? (
        <div className="zd-callout zd-callout--danger" style={{ margin: 0 }}>
          <strong>Delete everything this tool created.</strong>
          <p>Uses the id_map to surgically undo all resources from prior runs.</p>
          <button onClick={runCleanup} disabled={busy} style={btn("danger")} type="button">
            {busy ? "Running..." : "Confirm cleanup"}
          </button>
        </div>
      ) : null}

      {op === "rollback" ? (
        <div className="zd-callout zd-callout--warning" style={{ margin: 0, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <span>Phase:
            <select className="zd-input" style={{ marginLeft: 6 }} value={phase} onChange={(e) => setPhase(Number(e.target.value))}>
              <option value={1}>1 — Foundation</option>
              <option value={2}>2 — Business logic</option>
              <option value={3}>3 — Content</option>
            </select>
          </span>
          <button onClick={runRollback} disabled={busy} style={btn("danger")} type="button">
            {busy ? "Running..." : "Roll back"}
          </button>
        </div>
      ) : null}

      {op === "restore" ? (
        <div className="zd-stack" style={{ margin: 0 }}>
          {backups.length === 0 ? (
            <div className="zd-callout zd-callout--info" style={{ margin: 0 }}>No backups found. Run a migration first.</div>
          ) : (
            <>
              <div className="zd-choice-grid" style={{ gridTemplateColumns: "1fr" }}>
                {backups.map((b) => (
                  <label key={b.path} className={`zd-choice-card${selectedBackup === b.path ? " is-active" : ""}`}>
                    <input type="radio" name="dash-backup" checked={selectedBackup === b.path} onChange={() => setSelectedBackup(b.path)} />
                    <div>
                      <strong>{b.name}</strong>
                      <p>{b.resource_count != null ? `${b.resource_count} resources` : "Unknown"}</p>
                    </div>
                  </label>
                ))}
              </div>
              <div>
                <button onClick={runRestore} disabled={busy || !selectedBackup} style={btn(busy || !selectedBackup ? "disabled" : "danger")} type="button">
                  {busy ? "Running..." : "Restore"}
                </button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

function statusPillClass(phase: string): string {
  if (phase === "completed") {
    return "zd-status-pill--success";
  }
  if (phase === "failed") {
    return "zd-status-pill--danger";
  }
  if (phase === "cancelled") {
    return "zd-status-pill--warning";
  }
  return "zd-status-pill--neutral";
}

function colorFor(action: LogRecord["action"]): string {
  switch (action) {
    case "CREATED":
      return "#84edc1";
    case "PURGED":
      return "#ffd982";
    case "SKIPPED":
      return "#98aeb3";
    case "FAILED":
      return "#ffb3c0";
    case "MANUAL":
      return "#b5a7ff";
    case "NOTE":
      return "#daf2ee";
    default:
      return "#daf2ee";
  }
}
