import { useEffect, useMemo, useRef, useState } from "react";
import { cancelJob, getJobStatus, listBackups, startCleanup, startRestore, startRollback } from "../api/backend";
import { playError, playSuccess } from "../sound";
import { useStore } from "../state/store";
import { useToast } from "../toasts";
import type { LogRecord } from "../types";
import { btn } from "./PreFlight";
import { computeEstimate, formatDuration } from "./progressEstimate";

const POLL_INTERVAL_MS = 2000;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STARTUP_PHASES = new Set(["", "starting", "idle"]);

// UI keeps phases as numbers (1-5); the worker uses descriptive names.
// Mirrors the order in server/jobs.py:run_full_migration so the ETA
// can skip phases the operator didn't select.
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

  // The estimate is honest about what it knows: total elapsed,
  // per-phase elapsed, and ETA only when there's enough throughput
  // signal to justify a number. See progressEstimate.ts for the math.
  const selectedPhaseNames = useMemo(() => {
    const names = new Set<string>();
    for (const n of selectedPhases) {
      const name = PHASE_NUM_TO_NAME[n];
      if (name) names.add(name);
    }
    // "extract" and "format-target" always run conditionally inside the
    // worker, not via the selectedPhases list; treat them as included
    // for ETA purposes so they're counted in remaining-weight.
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
            <dt>Events</dt>
            <dd>{eventTail.length}</dd>
          </div>
          <div className="zd-summary-item" title="Wall-clock time since the worker picked up this job.">
            <dt>Elapsed</dt>
            <dd>{formatDuration(estimate.totalElapsedSec)}</dd>
          </div>
          <div className="zd-summary-item" title="Wall-clock time since the current phase started.">
            <dt>This phase</dt>
            <dd>{formatDuration(estimate.phaseElapsedSec)}</dd>
          </div>
          <div className="zd-summary-item" title={
            estimate.reason ??
            (estimate.itemsPerSec
              ? `Based on ${estimate.itemsPerSec.toFixed(2)} items/sec observed in this phase.`
              : "")
          }>
            <dt>ETA remaining</dt>
            <dd>
              {isTerminal ? (
                phase === "completed" ? "Done" : phase
              ) : estimate.etaSec === null ? (
                <span style={{ color: "#5d787f", fontStyle: "italic" }}>
                  {estimate.reason ?? "Estimating…"}
                </span>
              ) : (
                formatDuration(estimate.etaSec)
              )}
            </dd>
          </div>
        </div>

        {!isTerminal && estimate.etaSec !== null && estimate.phaseElapsedSec !== null ? (
          <ProgressBar
            elapsed={estimate.totalElapsedSec ?? 0}
            remaining={estimate.etaSec}
          />
        ) : null}
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
            targetConnectionId={targetConnectionId}
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

/**
 * Honest progress bar.
 *
 * The denominator is `elapsed + remaining` — both numbers come from
 * the same estimate, so the bar can't drift past 100 % while ETA is
 * being computed. When ETA shrinks (we're going faster than the
 * baseline), the bar accelerates; when it grows, the bar slows. We
 * never display this when ETA is null — the parent decides that.
 */
function ProgressBar({ elapsed, remaining }: { elapsed: number; remaining: number }) {
  const total = elapsed + remaining;
  const pct = total > 0 ? Math.min(100, Math.max(0, (elapsed / total) * 100)) : 0;
  return (
    <div style={{ marginTop: 12 }}>
      <div
        style={{
          height: 8,
          borderRadius: 4,
          background: "rgba(215, 226, 223, 0.45)",
          overflow: "hidden",
        }}
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Migration progress"
      >
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background: pct >= 95 ? "#ffd982" : "#84edc1",
            borderRadius: 4,
            transition: "width 800ms ease, background 600ms ease",
          }}
        />
      </div>
      <div style={{
        display: "flex", justifyContent: "space-between", marginTop: 6,
        fontSize: 12, color: "#5d787f",
      }}>
        <span>{formatDuration(elapsed)} elapsed</span>
        <span>{Math.round(pct)} %</span>
        <span>{formatDuration(remaining)} remaining</span>
      </div>
    </div>
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
