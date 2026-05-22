import { useCallback, useEffect, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import {
  downloadUrl,
  getReport,
  listBackups,
  listMigrations,
  startCleanup,
  startRestore,
  startRollback,
} from "../api/backend";
import { useStore } from "../state/store";
import { btn } from "./PreFlight";
import { useToast } from "../toasts";
import type { BackupInfo, MigrationInfo } from "../types";

// Configure once at module load. GFM gives us tables, task lists,
// strikethrough — exactly what the verify report uses.
marked.setOptions({
  gfm: true,
  breaks: false,
});

type OpTab = "report" | "cleanup" | "rollback" | "restore" | "history";

export function ReportRollback() {
  const setStep = useStore((s) => s.setStep);
  const reset = useStore((s) => s.reset);
  const migrationId = useStore((s) => s.currentMigrationId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);

  const [report, setReport] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [opTab, setOpTab] = useState<OpTab>("report");
  const [parseErr, setParseErr] = useState<string | null>(null);

  useEffect(() => {
    if (!migrationId) {
      setLoading(false);
      return;
    }
    setParseErr(null);
    void (async () => {
      try {
        setReport(await getReport(migrationId));
      } catch (error) {
        setErr(error instanceof Error ? error.message : String(error));
      } finally {
        setLoading(false);
      }
    })();
  }, [migrationId]);

  const [sanitisedHtml, setSanitisedHtml] = useState("");

  useEffect(() => {
    if (!report) {
      setSanitisedHtml("");
      setParseErr(null);
      return;
    }
    try {
      const raw = marked.parse(report, { async: false }) as string;
      setSanitisedHtml(DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } }));
      setParseErr(null);
    } catch (e) {
      setSanitisedHtml("");
      setParseErr(e instanceof Error ? e.message : String(e));
    }
  }, [report]);

  const reportContent = (
    <>
      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Migration report</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Final output from the verify phase, ready for operator review or
              archiving.
            </p>
          </div>
          {migrationId ? (
            <div className="zd-chip zd-chip--ghost">
              <b>Run</b>
              <span>{migrationId}</span>
            </div>
          ) : null}
        </div>
        {migrationId && !loading && !err ? (
          <DownloadActions migrationId={migrationId} reportMarkdown={report} />
        ) : null}
      </div>

      {loading ? (
        <div className="zd-empty-state">
          <div className="zd-status-pill zd-status-pill--neutral">Loading report</div>
          <h3>Fetching verify output</h3>
          <p style={{ marginBottom: 0 }}>
            Pulling the report from the backend host so it can be reviewed in
            this control center.
          </p>
        </div>
      ) : null}

      {err ? (
        <div className="zd-callout zd-callout--danger">
          <strong>Report unavailable:</strong> {err}
          {err.includes("not found") ? (
            <p style={{ margin: "10px 0 0" }}>
              The verify phase writes this file. If phase 4 was not selected,
              there may be no report to fetch.
            </p>
          ) : null}
        </div>
      ) : null}

      {parseErr ? (
        <div className="zd-callout zd-callout--danger">
          <strong>Report render error:</strong> {parseErr}
        </div>
      ) : null}

      {sanitisedHtml ? (
        <div
          className="zd-report"
          dangerouslySetInnerHTML={{ __html: sanitisedHtml }}
        />
      ) : null}
    </>
  );

  return (
    <div className="zd-stack">
      <div className="zd-section-tabs" style={{ paddingLeft: 0, paddingRight: 0 }}>
        <button
          className={`zd-section-tab${opTab === "report" ? " is-active" : ""}`}
          onClick={() => setOpTab("report")}
          type="button"
        >
          <strong>Report</strong>
          <span>Migration output</span>
        </button>
        <button
          className={`zd-section-tab${opTab === "cleanup" ? " is-active" : ""}`}
          onClick={() => setOpTab("cleanup")}
          type="button"
        >
          <strong>Cleanup</strong>
          <span>Full rollback</span>
        </button>
        <button
          className={`zd-section-tab${opTab === "rollback" ? " is-active" : ""}`}
          onClick={() => setOpTab("rollback")}
          type="button"
        >
          <strong>Rollback</strong>
          <span>Per-phase undo</span>
        </button>
        <button
          className={`zd-section-tab${opTab === "restore" ? " is-active" : ""}`}
          onClick={() => setOpTab("restore")}
          type="button"
        >
          <strong>Restore</strong>
          <span>From backup</span>
        </button>
        <button
          className={`zd-section-tab${opTab === "history" ? " is-active" : ""}`}
          onClick={() => setOpTab("history")}
          type="button"
        >
          <strong>History</strong>
          <span>Past runs</span>
        </button>
      </div>

      <div className="zd-tab-panel">
        {opTab === "report" ? reportContent : null}
        {opTab === "cleanup" ? <CleanupPanel targetConnectionId={targetConnectionId} /> : null}
        {opTab === "rollback" ? <RollbackPanel targetConnectionId={targetConnectionId} /> : null}
        {opTab === "restore" ? <RestorePanel targetConnectionId={targetConnectionId} /> : null}
        {opTab === "history" ? <HistoryPanel /> : null}
      </div>

      <div className="zd-inline-actions">
        <button
          onClick={() => {
            reset();
            setStep("preflight");
          }}
          style={btn("primary")}
          type="button"
        >
          Start another migration
        </button>
        <button onClick={() => setStep("progress")} style={btn("secondary")} type="button">
          Back to progress
        </button>
      </div>
    </div>
  );
}


/* ------------------------------------------------------------------ */
/*  Cleanup panel                                                      */
/* ------------------------------------------------------------------ */

function CleanupPanel({ targetConnectionId }: { targetConnectionId: string | null }) {
  const notify = useToast();
  const setStep = useStore((s) => s.setStep);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  async function handleCleanup() {
    if (!targetConnectionId) {
      notify({ tone: "danger", title: "No target connection", message: "Select a target connection first." });
      return;
    }
    setBusy(true);
    try {
      const resp = await startCleanup(targetConnectionId);
      setStep("progress");
      notify({ tone: "info", title: "Cleanup launched", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Cleanup failed", message: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  if (!confirm) {
    return (
      <div className="zd-callout zd-callout--danger" style={{ margin: 0 }}>
        <strong>Delete everything this tool created in the target.</strong>
        <p>Uses the stored id_map to surgically undo every resource that zd-transfer created during any prior run. State files are reset afterwards.</p>
        <div style={{ marginTop: 12 }}>
          <button onClick={() => setConfirm(true)} style={btn("danger")} type="button">
            I understand, continue
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="zd-empty-state">
      <h3>Confirm cleanup</h3>
      <p>This will delete all resources tracked in the id_map from the target account. This action cannot be undone — ensure you have a backup first.</p>
      <div className="zd-inline-actions" style={{ marginTop: 16 }}>
        <button onClick={() => setConfirm(false)} style={btn("secondary")} type="button">Cancel</button>
        <button onClick={handleCleanup} disabled={busy} style={btn(busy ? "disabled" : "danger")} type="button">
          {busy ? "Running..." : "Confirm cleanup"}
        </button>
      </div>
    </div>
  );
}


/* ------------------------------------------------------------------ */
/*  Rollback panel                                                     */
/* ------------------------------------------------------------------ */

function RollbackPanel({ targetConnectionId }: { targetConnectionId: string | null }) {
  const notify = useToast();
  const setStep = useStore((s) => s.setStep);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<number>(1);

  async function handleRollback() {
    if (!targetConnectionId) {
      notify({ tone: "danger", title: "No target connection", message: "Select a target connection first." });
      return;
    }
    setBusy(true);
    try {
      const resp = await startRollback(targetConnectionId, phase);
      setStep("progress");
      notify({ tone: "info", title: "Rollback launched", message: `Phase ${phase} rollback, tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Rollback failed", message: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="zd-stack">
      <div className="zd-callout zd-callout--warning" style={{ margin: 0 }}>
        <strong>Roll back one specific phase.</strong>
        <p>Deletes only the resources that were created by the selected phase. Leaves other phases intact.</p>
      </div>
      <label className="zd-field" style={{ marginTop: 0 }}>
        <span>Phase to roll back</span>
        <select className="zd-input" value={phase} onChange={(e) => setPhase(Number(e.target.value))}>
          <option value={1}>1 — Foundation</option>
          <option value={2}>2 — Business logic</option>
          <option value={3}>3 — Content</option>
        </select>
      </label>
      <div className="zd-inline-actions">
        <button onClick={handleRollback} disabled={busy} style={btn(busy ? "disabled" : "danger")} type="button">
          {busy ? "Running..." : `Roll back phase ${phase}`}
        </button>
      </div>
    </div>
  );
}


/* ------------------------------------------------------------------ */
/*  Restore panel                                                      */
/* ------------------------------------------------------------------ */

function RestorePanel({ targetConnectionId }: { targetConnectionId: string | null }) {
  const notify = useToast();
  const setStep = useStore((s) => s.setStep);
  const [busy, setBusy] = useState(false);
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [loadingBackups, setLoadingBackups] = useState(true);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setBackups(await listBackups());
      } catch (error) {
        notify({ tone: "danger", title: "Cannot list backups", message: error instanceof Error ? error.message : String(error) });
      } finally {
        setLoadingBackups(false);
      }
    })();
  }, [notify]);

  async function handleRestore() {
    if (!targetConnectionId || !selectedPath) return;
    setBusy(true);
    try {
      const resp = await startRestore(targetConnectionId, selectedPath);
      setStep("progress");
      notify({ tone: "info", title: "Restore launched", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Restore failed", message: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }

  if (loadingBackups) {
    return <div className="zd-empty-state"><h3>Loading backups...</h3></div>;
  }

  if (!confirm) {
    return (
      <div className="zd-stack">
        {backups.length === 0 ? (
          <div className="zd-empty-state">
            <h3>No backups found</h3>
            <p>Run a migration first — backups are created automatically before format+write operations.</p>
          </div>
        ) : (
          <>
            <div className="zd-callout zd-callout--warning" style={{ margin: 0 }}>
              <strong>Select a backup to restore.</strong>
              <p>Resources will be re-created in the target account. Names that already exist will be skipped.</p>
            </div>
            <div className="zd-choice-grid" style={{ gridTemplateColumns: "1fr" }}>
              {backups.map((b) => (
                <label key={b.path} className={`zd-choice-card${selectedPath === b.path ? " is-active" : ""}`}>
                  <input
                    type="radio"
                    name="backup"
                    checked={selectedPath === b.path}
                    onChange={() => setSelectedPath(b.path)}
                  />
                  <div>
                    <strong>{b.name}</strong>
                    <p>{b.resource_count != null ? `${b.resource_count} resources` : "Unknown size"}</p>
                  </div>
                </label>
              ))}
            </div>
            <div className="zd-inline-actions">
              <button
                onClick={() => setConfirm(true)}
                disabled={!selectedPath}
                style={btn(!selectedPath ? "disabled" : "danger")}
                type="button"
              >
                Continue to restore
              </button>
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="zd-empty-state">
      <h3>Confirm restore</h3>
      <p>This will create resources in the target account from the backup <strong>{selectedPath}</strong>. Ensure the target is in a clean state or that name conflicts are acceptable.</p>
      <div className="zd-inline-actions" style={{ marginTop: 16 }}>
        <button onClick={() => setConfirm(false)} style={btn("secondary")} type="button">Cancel</button>
        <button onClick={handleRestore} disabled={busy} style={btn(busy ? "disabled" : "danger")} type="button">
          {busy ? "Running..." : "Confirm restore"}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  History panel                                                      */
/* ------------------------------------------------------------------ */

function HistoryPanel() {
  const [migrations, setMigrations] = useState<MigrationInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selectedMid, setSelectedMid] = useState<string | null>(null);
  const [historyReport, setHistoryReport] = useState<string | null>(null);
  const [historyErr, setHistoryErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setMigrations(await listMigrations());
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const loadReport = useCallback(async (mid: string) => {
    setSelectedMid(mid);
    setHistoryErr(null);
    try {
      const md = await getReport(mid);
      const raw = marked.parse(md, { async: false }) as string;
      setHistoryReport(DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } }));
    } catch (e) {
      setHistoryErr(e instanceof Error ? e.message : String(e));
      setHistoryReport(null);
    }
  }, []);

  if (loading) {
    return <div className="zd-empty-state"><h3>Loading history...</h3></div>;
  }

  if (err) {
    return <div className="zd-callout zd-callout--danger"><strong>Error:</strong> {err}</div>;
  }

  if (selectedMid && (historyReport || historyErr)) {
    return (
      <div className="zd-stack">
        <div className="zd-inline-actions">
          <button onClick={() => { setSelectedMid(null); setHistoryReport(null); setHistoryErr(null); }} style={btn("ghost")} type="button">
            &larr; Back to history
          </button>
        </div>
        {historyErr ? (
          <div className="zd-callout zd-callout--warning"><strong>Report unavailable:</strong> {historyErr}</div>
        ) : null}
        {historyReport ? (
          <div className="zd-report" dangerouslySetInnerHTML={{ __html: historyReport }} />
        ) : null}
      </div>
    );
  }

  if (migrations.length === 0) {
    return (
      <div className="zd-empty-state">
        <h3>No past runs</h3>
        <p style={{ marginBottom: 0 }}>Completed migrations will appear here for 3 days.</p>
      </div>
    );
  }

  return (
    <div className="zd-table-wrap">
      <table className="zd-table">
        <thead>
          <tr>
            <th>Run ID</th>
            <th>Date</th>
            <th>Status</th>
            <th>Report</th>
            <th>Log</th>
          </tr>
        </thead>
        <tbody>
          {migrations.map((m) => (
            <tr key={m.migration_id}>
              <td><code>{m.migration_id}</code></td>
              <td>{new Date(m.created_at).toLocaleString()}</td>
              <td><span className={`zd-status-pill ${statusPillClass(m.phase)}`}>{m.phase}</span></td>
              <td>
                {m.has_report ? (
                  <button onClick={() => loadReport(m.migration_id)} style={btn("ghost")} type="button">View</button>
                ) : <span style={{ color: "#68737d" }}>—</span>}
              </td>
              <td>
                {m.has_log ? (
                  <a href={downloadUrl(`/migrations/${encodeURIComponent(m.migration_id)}/log`)} style={btn("ghost")} download>Download</a>
                ) : <span style={{ color: "#68737d" }}>—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


function DownloadActions({
  migrationId,
  reportMarkdown,
}: {
  migrationId: string;
  reportMarkdown: string | null;
}) {
  function downloadHtml() {
    if (!reportMarkdown) return;
    const html = wrapForExport(
      DOMPurify.sanitize(marked.parse(reportMarkdown, { async: false }) as string),
      migrationId,
    );
    triggerBlobDownload(
      `migration_report_${migrationId}.html`,
      html,
      "text/html;charset=utf-8",
    );
  }

  function printReport() {
    if (!reportMarkdown) return;
    const html = wrapForExport(
      DOMPurify.sanitize(marked.parse(reportMarkdown, { async: false }) as string),
      migrationId,
    );
    // Open in a hidden window so we can give it the print stylesheet
    // without touching the iframe's own DOM.
    const w = window.open("", "_blank", "noopener");
    if (!w) {
      alert("Popup was blocked. Allow popups for this site to print/PDF.");
      return;
    }
    w.document.write(html);
    w.document.close();
    // Wait for the new window's stylesheets to settle, then trigger
    // the print dialog. "Save as PDF" lives inside the OS print dialog.
    w.onload = () => {
      w.focus();
      w.print();
    };
  }

  return (
    <div className="zd-inline-actions" style={{ marginTop: 16, gap: 8 }}>
      <a
        href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/report`)}
        style={btn("secondary")}
        download
      >
        Download .md
      </a>
      <button onClick={downloadHtml} style={btn("secondary")} type="button">
        Download .html
      </button>
      <button onClick={printReport} style={btn("secondary")} type="button">
        Print / Save as PDF
      </button>
      <a
        href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/log`)}
        style={btn("ghost")}
        download
      >
        Audit log (.jsonl)
      </a>
      <a
        href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/id-map`)}
        style={btn("ghost")}
        download
      >
        ID map (.json)
      </a>
    </div>
  );
}

/** Wrap sanitised HTML in a printable, standalone document. */
function wrapForExport(bodyHtml: string, migrationId: string): string {
  const css = `
    body { font: 14px/1.5 -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
           color: #1f1f1f; max-width: 920px; margin: 32px auto; padding: 0 24px; }
    h1, h2, h3 { color: #03363d; }
    h1 { border-bottom: 2px solid #03363d; padding-bottom: 6px; }
    h2 { margin-top: 32px; border-bottom: 1px solid #e9ebed; padding-bottom: 4px; }
    table { border-collapse: collapse; margin: 12px 0; width: 100%; }
    th, td { border: 1px solid #d8dcde; padding: 6px 10px; text-align: left;
             font-size: 13px; vertical-align: top; }
    th { background: #f8f9f9; }
    code { background: #f1f3f3; padding: 1px 4px; border-radius: 3px;
           font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
    pre { background: #f8f9f9; padding: 12px; border-radius: 4px; overflow-x: auto; }
    ul { padding-left: 20px; }
    li { margin: 2px 0; }
    .meta { color: #68737d; font-size: 12px; margin-bottom: 24px; }
    @media print {
      body { margin: 0; max-width: 100%; padding: 16mm; }
      a[href]:after { content: " (" attr(href) ")"; font-size: 10px; color: #68737d; }
    }
  `;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Migration report — ${escapeHtml(migrationId)}</title>
<style>${css}</style>
</head>
<body>
<div class="meta">Run <code>${escapeHtml(migrationId)}</code> — exported ${new Date().toISOString()}</div>
${bodyHtml}
</body>
</html>`;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] || c),
  );
}

function triggerBlobDownload(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Free the object URL on the next tick.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function statusPillClass(phase: string): string {
  if (phase === "completed") return "zd-status-pill--success";
  if (phase === "failed") return "zd-status-pill--danger";
  if (phase === "cancelled") return "zd-status-pill--warning";
  return "zd-status-pill--neutral";
}
