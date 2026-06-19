import { useCallback, useEffect, useMemo, useState } from "react";
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
import { useToast } from "../toasts";
import type { BackupInfo, MigrationInfo } from "../types";

marked.setOptions({ gfm: true, breaks: false });

type OpTab = "report" | "history";
const REPORT_RETRY_DELAY_MS = 1500;
const REPORT_MAX_RETRIES = 8;

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
  const [retryCount, setRetryCount] = useState(0);
  const [undoOpen, setUndoOpen] = useState(false);

  useEffect(() => {
    if (!migrationId) {
      setReport(null); setErr(null); setParseErr(null); setLoading(false);
      return;
    }
    let cancelled = false;
    let timer: number | null = null;
    let attempts = 0;
    setLoading(true); setErr(null); setReport(null); setParseErr(null); setRetryCount(0);

    const load = async () => {
      try {
        const next = await getReport(migrationId);
        if (cancelled) return;
        if (!next.trim() && attempts < REPORT_MAX_RETRIES) {
          attempts += 1; setRetryCount(attempts);
          timer = window.setTimeout(() => void load(), REPORT_RETRY_DELAY_MS);
          return;
        }
        setReport(next);
        setErr(next.trim() ? null : "Report is empty.");
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        if (shouldRetry(message) && attempts < REPORT_MAX_RETRIES) {
          attempts += 1; setRetryCount(attempts);
          timer = window.setTimeout(() => void load(), REPORT_RETRY_DELAY_MS);
          return;
        }
        setErr(message);
      } finally {
        if (cancelled || timer !== null) return;
        setLoading(false);
      }
    };

    void load();
    return () => { cancelled = true; if (timer !== null) window.clearTimeout(timer); };
  }, [migrationId]);

  useEffect(() => {
    if (!report) { setParseErr(null); return; }
    try {
      marked.parse(report, { async: false });
      setParseErr(null);
    } catch (e) {
      setParseErr(e instanceof Error ? e.message : String(e));
    }
  }, [report]);

  return (
    <div className="zd-stack">
      {/* Tabs: Report / History */}
      <div className="zd-section-tabs" style={{ paddingLeft: 0, paddingRight: 0 }}>
        <button
          className={`zd-section-tab${opTab === "report" ? " is-active" : ""}`}
          onClick={() => setOpTab("report")}
          type="button"
        >
          <strong>Report</strong>
          <span>What was copied</span>
        </button>
        <button
          className={`zd-section-tab${opTab === "history" ? " is-active" : ""}`}
          onClick={() => setOpTab("history")}
          type="button"
        >
          <strong>History</strong>
          <span>Past transfers</span>
        </button>
      </div>

      <div className="zd-tab-panel">
        {opTab === "report" ? (
          <ReportContent
            migrationId={migrationId}
            report={report}
            loading={loading}
            err={err}
            retryCount={retryCount}
            parseErr={parseErr}
            setOpTab={setOpTab}
          />
        ) : (
          <HistoryPanel />
        )}
      </div>

      {/* Undo actions section — expandable, grouped by severity */}
      {migrationId ? (
        <div className="zd-undo-section">
          <div
            className={`zd-undo-header${undoOpen ? " is-open" : ""}`}
            onClick={() => setUndoOpen(!undoOpen)}
          >
            <strong>Undo actions</strong>
            <span className="zd-undo-arrow">{">"}</span>
          </div>
          {undoOpen ? (
            <div className="zd-undo-body">
              <UndoPanel targetConnectionId={targetConnectionId} setStep={setStep} />
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="zd-inline-actions">
        <button
          onClick={() => { reset(); setStep("preflight"); }}
          className="zd-button zd-button--primary"
          type="button"
        >
          Start another transfer
        </button>
        {migrationId ? (
          <button onClick={() => setStep("progress")} className="zd-button zd-button--secondary" type="button">
            Back to progress
          </button>
        ) : null}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Report Sections — tabbed + collapsible                            */
/* ------------------------------------------------------------------ */

type SectionTone = "neutral" | "info" | "success" | "warning" | "danger";

type SectionMeta = {
  shortLabel: string;
  tone: SectionTone;
  defaultOpen: boolean;
};

type ReportSection = {
  id: string;
  title: string;
  html: string;
  count: number | null;
  meta: SectionMeta;
};

const SECTION_META: { match: string; meta: SectionMeta }[] = [
  { match: "summary", meta: { shortLabel: "Summary", tone: "neutral", defaultOpen: true } },
  { match: "resource count verification", meta: { shortLabel: "Verify", tone: "info", defaultOpen: true } },
  { match: "skipped resources", meta: { shortLabel: "Skipped", tone: "warning", defaultOpen: false } },
  { match: "purged resources", meta: { shortLabel: "Purged", tone: "danger", defaultOpen: false } },
  { match: "failed resources", meta: { shortLabel: "Failed", tone: "danger", defaultOpen: false } },
  { match: "manual action", meta: { shortLabel: "Manual", tone: "warning", defaultOpen: false } },
  { match: "cutover checklist", meta: { shortLabel: "Checklist", tone: "info", defaultOpen: false } },
];

const DEFAULT_META: SectionMeta = { shortLabel: "Other", tone: "neutral", defaultOpen: false };

function getSectionMeta(title: string): SectionMeta {
  const lower = title.toLowerCase();
  for (const entry of SECTION_META) {
    if (lower.includes(entry.match)) return entry.meta;
  }
  return DEFAULT_META;
}

function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function countSectionItems(body: string): number | null {
  const lines = body.split("\n");
  const tableRows = lines.filter((l) => {
    const t = l.trim();
    return t.startsWith("|") && !/^\|[\s:|-]+\|?$/.test(t);
  });
  if (tableRows.length > 2) return tableRows.length - 2;
  const listItems = lines.filter((l) => /^\s*[-*]\s/.test(l));
  if (listItems.length > 0) return listItems.length;
  return null;
}

function parseReportSections(markdown: string): ReportSection[] {
  const lines = markdown.split("\n");
  const sections: ReportSection[] = [];
  let currentTitle = "";
  let currentBody: string[] = [];

  const flush = () => {
    if (!currentTitle) return;
    const body = currentBody.join("\n").trim();
    if (!body) return;
    const meta = getSectionMeta(currentTitle);
    const html = DOMPurify.sanitize(
      marked.parse(body, { async: false }) as string,
      { USE_PROFILES: { html: true } },
    );
    sections.push({
      id: slugify(currentTitle),
      title: currentTitle,
      html,
      count: countSectionItems(body),
      meta,
    });
  };

  for (const line of lines) {
    if (line.startsWith("## ")) {
      flush();
      currentTitle = line.slice(3).trim();
      currentBody = [];
    } else if (currentTitle) {
      currentBody.push(line);
    }
  }
  flush();
  return sections;
}

function ReportSections({ markdown }: { markdown: string }) {
  const sections = useMemo(() => parseReportSections(markdown), [markdown]);
  const [activeTab, setActiveTab] = useState<string>("all");
  const [openSections, setOpenSections] = useState<Set<string>>(() => {
    const open = new Set<string>();
    sections.forEach((s) => { if (s.meta.defaultOpen) open.add(s.id); });
    return open;
  });

  useEffect(() => {
    const open = new Set<string>();
    sections.forEach((s) => { if (s.meta.defaultOpen) open.add(s.id); });
    setOpenSections(open);
    setActiveTab("all");
  }, [sections]);

  const toggleSection = (id: string) => {
    setOpenSections((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const visibleSections = activeTab === "all"
    ? sections
    : sections.filter((s) => s.id === activeTab);

  return (
    <div className="zd-report-sections">
      <div className="zd-report-tabs">
        <button
          className={`zd-report-tab${activeTab === "all" ? " is-active" : ""}`}
          onClick={() => setActiveTab("all")}
          type="button"
        >
          All
        </button>
        {sections.map((s) => (
          <button
            key={s.id}
            className={`zd-report-tab zd-report-tab--${s.meta.tone}${activeTab === s.id ? " is-active" : ""}`}
            onClick={() => setActiveTab(s.id)}
            type="button"
          >
            {s.meta.shortLabel}
            {s.count != null ? <span className="zd-report-tab-count">{s.count}</span> : null}
          </button>
        ))}
      </div>

      <div className="zd-report-cards">
        {visibleSections.map((s) => {
          const isOpen = activeTab === "all" ? openSections.has(s.id) : true;
          return (
            <div key={s.id} className={`zd-report-section${isOpen ? " is-open" : ""}`}>
              <div
                className="zd-report-section-header"
                onClick={() => activeTab === "all" && toggleSection(s.id)}
                role={activeTab === "all" ? "button" : undefined}
              >
                <span className={`zd-report-section-dot zd-report-section-dot--${s.meta.tone}`} />
                <strong>{s.title}</strong>
                {s.count != null ? <span className="zd-report-section-count">{s.count}</span> : null}
                {activeTab === "all" ? (
                  <span className="zd-report-section-arrow">{isOpen ? "\u25BE" : "\u25B8"}</span>
                ) : null}
              </div>
              {isOpen ? (
                <div className="zd-report-section-body zd-report" dangerouslySetInnerHTML={{ __html: s.html }} />
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Report Content                                                     */
/* ------------------------------------------------------------------ */

function ReportContent({
  migrationId, report, loading, err, retryCount, parseErr, setOpTab,
}: {
  migrationId: string | null;
  report: string | null;
  loading: boolean;
  err: string | null;
  retryCount: number;
  parseErr: string | null;
  setOpTab: (t: "report" | "history") => void;
}) {
  return (
    <div className="zd-stack">
      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Transfer report</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              {migrationId
                ? "Here's what happened during the transfer."
                : "No transfer has been completed yet."}
            </p>
          </div>
          {migrationId ? (
            <span className="zd-chip zd-chip--ghost">
              <b>Run</b>
              <span>{migrationId}</span>
            </span>
          ) : null}
        </div>
        {migrationId && !loading && !err ? (
          <DownloadActions migrationId={migrationId} reportMarkdown={report} />
        ) : null}
      </div>

      {loading ? (
        <div className="zd-empty-state">
          <div className="zd-orbit-loader" aria-hidden="true">
            <span /><span /><span />
          </div>
          <h3>Loading report</h3>
          <p>{retryCount > 0 ? "Waiting for the report to be ready..." : "Fetching the transfer report."}</p>
        </div>
      ) : null}

      {!loading && !migrationId ? (
        <div className="zd-empty-state">
          <h3>No report yet</h3>
          <p>Run a transfer first to see the results here.</p>
        </div>
      ) : null}

      {err ? (
        <div className="zd-callout zd-callout--danger">
          <strong>Report not available:</strong> {err}
          <div className="zd-inline-actions" style={{ marginTop: 12 }}>
            <button onClick={() => setOpTab("history")} className="zd-button zd-button--secondary" type="button">Browse past reports</button>
          </div>
        </div>
      ) : null}

      {parseErr ? <div className="zd-callout zd-callout--danger"><strong>Render error:</strong> {parseErr}</div> : null}

      {report ? (
        <ReportSections markdown={report} />
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Undo Panel — grouped by psychological severity                     */
/* ------------------------------------------------------------------ */

function UndoPanel({
  targetConnectionId,
  setStep,
}: {
  targetConnectionId: string | null;
  setStep: (s: import("../types").WizardStep) => void;
}) {
  const [phase, setPhase] = useState(1);
  const [selectedBackup, setSelectedBackup] = useState<string | null>(null);
  const [backups, setBackups] = useState<BackupInfo[]>([]);
  const [loadingBackups, setLoadingBackups] = useState(true);
  const [busyRollback, setBusyRollback] = useState(false);
  const [busyRestore, setBusyRestore] = useState(false);
  const [busyCleanup, setBusyCleanup] = useState(false);
  const notify = useToast();

  useEffect(() => {
    void (async () => {
      try { setBackups(await listBackups()); }
      catch (error) { notify({ tone: "danger", title: "Cannot list backups", message: error instanceof Error ? error.message : String(error) }); }
      finally { setLoadingBackups(false); }
    })();
  }, [notify]);

  async function handleRollback() {
    if (!targetConnectionId) return;
    setBusyRollback(true);
    try {
      const resp = await startRollback(targetConnectionId, phase);
      setStep("progress");
      notify({ tone: "info", title: "Rollback started", message: `Phase ${phase}, tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Rollback failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusyRollback(false); }
  }

  async function handleRestore() {
    if (!targetConnectionId || !selectedBackup) return;
    setBusyRestore(true);
    try {
      const resp = await startRestore(targetConnectionId, selectedBackup);
      setStep("progress");
      notify({ tone: "info", title: "Restore started", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Restore failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusyRestore(false); }
  }

  async function handleCleanup() {
    if (!targetConnectionId) return;
    setBusyCleanup(true);
    try {
      const resp = await startCleanup(targetConnectionId);
      setStep("progress");
      notify({ tone: "info", title: "Cleanup started", message: `Tracking run ${resp.migration_id}.` });
    } catch (error) {
      notify({ tone: "danger", title: "Cleanup failed", message: error instanceof Error ? error.message : String(error) });
    } finally { setBusyCleanup(false); }
  }

  return (
    <div className="zd-stack" style={{ gap: 14 }}>
      {/* 1. Rollback — safest */}
      <div className="zd-card" style={{ boxShadow: "var(--shadow-xs)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
          <div>
            <h4 style={{ margin: 0, fontSize: 14 }}>Undo one phase</h4>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--text-soft)" }}>
              Remove settings that were copied in a specific phase. Other changes stay.
            </p>
          </div>
          <span className="zd-risk-badge zd-risk-badge--safe">Safe</span>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <select className="zd-input" style={{ minHeight: 36, width: "auto", minWidth: 200 }} value={phase} onChange={(e) => setPhase(Number(e.target.value))}>
            <option value={1}>1 — Settings & Structure</option>
            <option value={2}>2 — Rules & Automation</option>
            <option value={3}>3 — Help Center</option>
          </select>
          <button onClick={handleRollback} disabled={busyRollback} className="zd-button zd-button--secondary" type="button">
            {busyRollback ? "Running..." : "Undo this phase"}
          </button>
        </div>
      </div>

      {/* 2. Restore — moderate */}
      <div className="zd-card" style={{ boxShadow: "var(--shadow-xs)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
          <div>
            <h4 style={{ margin: 0, fontSize: 14 }}>Restore from backup</h4>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--text-soft)" }}>
              Re-create settings from a backup that was taken before a transfer.
            </p>
          </div>
          <span className="zd-risk-badge zd-risk-badge--moderate">Moderate</span>
        </div>
        {loadingBackups ? (
          <span style={{ fontSize: 12, color: "var(--text-faint)" }}>Loading backups...</span>
        ) : backups.length === 0 ? (
          <span style={{ fontSize: 12, color: "var(--text-faint)" }}>No backups available.</span>
        ) : (
          <div className="zd-stack" style={{ gap: 8 }}>
            <div className="zd-choice-grid" style={{ gridTemplateColumns: "1fr" }}>
              {backups.map((b) => (
                <label key={b.path} className={`zd-choice-card${selectedBackup === b.path ? " is-active" : ""}`} style={{ padding: "8px 12px" }}>
                  <input type="radio" name="undo-backup" checked={selectedBackup === b.path} onChange={() => setSelectedBackup(b.path)} />
                  <div>
                    <strong style={{ fontSize: 13 }}>{b.name}</strong>
                    <p style={{ fontSize: 11 }}>{b.resource_count != null ? `${b.resource_count} resources` : ""}</p>
                  </div>
                </label>
              ))}
            </div>
            <button onClick={handleRestore} disabled={busyRestore || !selectedBackup} className="zd-button zd-button--secondary" type="button" style={{ alignSelf: "flex-start" }}>
              {busyRestore ? "Running..." : "Restore this backup"}
            </button>
          </div>
        )}
      </div>

      {/* 3. Cleanup — most destructive */}
      <div className="zd-card" style={{ boxShadow: "var(--shadow-xs)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
          <div>
            <h4 style={{ margin: 0, fontSize: 14 }}>Delete everything</h4>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--text-soft)" }}>
              Remove all settings that were copied by this tool. Uses the internal tracking to undo everything.
            </p>
          </div>
          <span className="zd-risk-badge zd-risk-badge--destructive">Destructive</span>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <button onClick={handleCleanup} disabled={busyCleanup} className="zd-button zd-button--danger" type="button">
            {busyCleanup ? "Running..." : "Delete all copied settings"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  History Panel                                                      */
/* ------------------------------------------------------------------ */

function HistoryPanel() {
  const [migrations, setMigrations] = useState<MigrationInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selectedMid, setSelectedMid] = useState<string | null>(null);
  const [historyMarkdown, setHistoryMarkdown] = useState<string | null>(null);
  const [historyErr, setHistoryErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try { setMigrations(await listMigrations()); }
      catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
      finally { setLoading(false); }
    })();
  }, []);

  const loadReport = useCallback(async (mid: string) => {
    setSelectedMid(mid); setHistoryErr(null); setHistoryMarkdown(null);
    try {
      const md = await getReport(mid);
      setHistoryMarkdown(md);
    } catch (e) {
      setHistoryErr(e instanceof Error ? e.message : String(e));
      setHistoryMarkdown(null);
    }
  }, []);

  if (loading) return <div className="zd-empty-state"><h3>Loading history...</h3></div>;
  if (err) return <div className="zd-callout zd-callout--danger"><strong>Error:</strong> {err}</div>;

  if (selectedMid && (historyMarkdown || historyErr)) {
    return (
      <div className="zd-stack">
        <button onClick={() => { setSelectedMid(null); setHistoryMarkdown(null); setHistoryErr(null); }} className="zd-button zd-button--ghost" type="button" style={{ alignSelf: "flex-start" }}>
          &larr; Back to history
        </button>
        {historyErr ? <div className="zd-callout zd-callout--warning"><strong>Unavailable:</strong> {historyErr}</div> : null}
        {historyMarkdown ? <ReportSections markdown={historyMarkdown} /> : null}
      </div>
    );
  }

  if (migrations.length === 0) {
    return <div className="zd-empty-state"><h3>No past transfers</h3><p>Completed transfers appear here for 3 days.</p></div>;
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
                  <button onClick={() => loadReport(m.migration_id)} className="zd-button zd-button--ghost" type="button">View</button>
                ) : <span style={{ color: "var(--text-faint)" }}>—</span>}
              </td>
              <td>
                {m.has_log ? (
                  <a href={downloadUrl(`/migrations/${encodeURIComponent(m.migration_id)}/log`)} className="zd-button zd-button--ghost" download>Download</a>
                ) : <span style={{ color: "var(--text-faint)" }}>—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Download Actions                                                   */
/* ------------------------------------------------------------------ */

function DownloadActions({ migrationId, reportMarkdown }: { migrationId: string; reportMarkdown: string | null }) {
  function downloadHtml() {
    if (!reportMarkdown) return;
    const html = wrapForExport(
      DOMPurify.sanitize(marked.parse(reportMarkdown, { async: false }) as string),
      migrationId,
    );
    triggerBlobDownload(`migration_report_${migrationId}.html`, html, "text/html;charset=utf-8");
  }

  function printReport() {
    if (!reportMarkdown) return;
    const html = wrapForExport(
      DOMPurify.sanitize(marked.parse(reportMarkdown, { async: false }) as string),
      migrationId,
    );
    // Use a hidden iframe to avoid popup blockers.
    const iframe = document.createElement("iframe");
    iframe.style.cssText = "position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;border:none";
    iframe.srcdoc = html;
    document.body.appendChild(iframe);
    iframe.onload = () => {
      try {
        iframe.contentWindow?.focus();
        iframe.contentWindow?.print();
      } catch {
        // Fallback: if iframe printing fails, try window.open.
        const w = window.open("", "_blank", "noopener");
        if (w) { w.document.write(html); w.document.close(); w.print(); }
      }
      // Remove iframe after a short delay to let the print dialog open.
      setTimeout(() => document.body.removeChild(iframe), 1000);
    };
  }

  return (
    <div className="zd-inline-actions" style={{ marginTop: 12, gap: 8 }}>
      <a href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/report`)} className="zd-button zd-button--secondary" download>Download .md</a>
      <button onClick={downloadHtml} className="zd-button zd-button--secondary" type="button">Download .html</button>
      <button onClick={printReport} className="zd-button zd-button--ghost" type="button">Print / PDF</button>
      <a href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/log`)} className="zd-button zd-button--ghost" download>Audit log (.jsonl)</a>
      <a href={downloadUrl(`/migrations/${encodeURIComponent(migrationId)}/id-map`)} className="zd-button zd-button--ghost" download>ID map (.json)</a>
    </div>
  );
}

function wrapForExport(bodyHtml: string, migrationId: string): string {
  const css = `
    body { font: 14px/1.5 -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
           color: #1f1f1f; max-width: 920px; margin: 32px auto; padding: 0 24px; }
    h1, h2, h3 { color: #1a1a1a; }
    h1 { border-bottom: 2px solid #2f8fa8; padding-bottom: 6px; }
    h2 { margin-top: 32px; border-bottom: 1px solid #e8e8e7; padding-bottom: 4px; }
    table { border-collapse: collapse; margin: 12px 0; width: 100%; }
    th, td { border: 1px solid #e8e8e7; padding: 6px 10px; text-align: left;
             font-size: 13px; vertical-align: top; }
    th { background: #f8f8f7; }
    code { background: #f5f6f7; padding: 1px 4px; border-radius: 3px;
           font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
    pre { background: #f8f8f7; padding: 12px; border-radius: 4px; overflow-x: auto; }
    ul { padding-left: 20px; }
    li { margin: 2px 0; }
    .meta { color: #9a9a9a; font-size: 12px; margin-bottom: 24px; }
    @media print { body { margin: 0; max-width: 100%; padding: 16mm; } }
  `;
  return `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Report — ${escapeHtml(migrationId)}</title><style>${css}</style></head>
<body><div class="meta">Run <code>${escapeHtml(migrationId)}</code> — ${new Date().toISOString()}</div>${bodyHtml}</body></html>`;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] || c));
}

function triggerBlobDownload(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function statusPillClass(phase: string): string {
  if (phase === "completed") return "zd-status-pill--success";
  if (phase === "failed") return "zd-status-pill--danger";
  if (phase === "cancelled") return "zd-status-pill--warning";
  return "zd-status-pill--neutral";
}

function shouldRetry(message: string): boolean {
  return message.toLowerCase().includes("report not found");
}
