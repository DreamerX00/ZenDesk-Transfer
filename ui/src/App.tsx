import { useEffect, useRef, useState } from "react";
import { boot } from "./boot";
import { isMuted, setMuted } from "./sound";
import { ThemeToggle } from "./ThemeToggle";
import { useStore } from "./state/store";
import { PreFlight } from "./steps/PreFlight";
import { SourceAuth } from "./steps/SourceAuth";
import { ChoosePhases } from "./steps/ChoosePhases";
import { PreviewConfirm } from "./steps/PreviewConfirm";
import { ProgressDashboard } from "./steps/ProgressDashboard";
import { ReportRollback } from "./steps/ReportRollback";
import { useToast } from "./toasts";
import type { MaskedConnection, WizardStep } from "./types";

interface StepMeta {
  key: WizardStep;
  label: string;
  eyebrow: string;
  title: string;
  description: string;
}

type ChipTone =
  | "brand"
  | "accent"
  | "success"
  | "danger"
  | "warning"
  | "info"
  | "ghost";

const STEPS: StepMeta[] = [
  {
    key: "preflight",
    label: "Status",
    eyebrow: "Get started",
    title: "Check that everything is ready to go",
    description:
      "See if your Zendesk accounts are connected and working before you start copying settings.",
  },
  {
    key: "source-auth",
    label: "Connect",
    eyebrow: "Accounts",
    title: "Link your source and target workspaces",
    description:
      "Connect the Zendesk account you want to copy from, and the one you want to copy to.",
  },
  {
    key: "choose-phases",
    label: "What to copy",
    eyebrow: "Choose",
    title: "Pick what gets moved and how it runs",
    description:
      "Decide which settings to copy, how many users to include, and whether to do a dry run first.",
  },
  {
    key: "preview-confirm",
    label: "Review",
    eyebrow: "Review",
    title: "Check everything before we start",
    description:
      "A quick look at your choices — workspaces, phases, and run mode — before the transfer begins.",
  },
  {
    key: "progress",
    label: "Watch",
    eyebrow: "Running",
    title: "Watch your transfer happen live",
    description:
      "See real-time progress as each piece of your Zendesk configuration is copied over.",
  },
  {
    key: "report",
    label: "Done",
    eyebrow: "Results",
    title: "See what was copied and what's next",
    description:
      "Review the final report, undo any changes if needed, or start a new transfer.",
  },
];

const STEP_MAP = Object.fromEntries(
  STEPS.map((step) => [step.key, step]),
) as Record<WizardStep, StepMeta>;

export default function App() {
  const step = useStore((s) => s.step);
  const setStep = useStore((s) => s.setStep);
  const bootError = useStore((s) => s.bootError);
  const bearer = useStore((s) => s.bearer);
  const dryRun = useStore((s) => s.dryRun);
  const formatTarget = useStore((s) => s.formatTarget);
  const selectedPhases = useStore((s) => s.selectedPhases);
  const currentMigrationId = useStore((s) => s.currentMigrationId);
  const sourceConnections = useStore((s) => s.sourceConnections);
  const targetConnections = useStore((s) => s.targetConnections);
  const sourceConnectionId = useStore((s) => s.sourceConnectionId);
  const targetConnectionId = useStore((s) => s.targetConnectionId);
  const jobStatus = useStore((s) => s.jobStatus);

  const notify = useToast();
  const [muted, setMutedState] = useState<boolean>(() => isMuted());
  const readyForToasts = useRef(false);
  const previousStep = useRef(step);
  const previousSource = useRef(sourceConnectionId);
  const previousTarget = useRef(targetConnectionId);
  const previousPhase = useRef(jobStatus.phase ?? null);

  useEffect(() => {
    void boot();
  }, []);

  useEffect(() => {
    if (!bearer || readyForToasts.current) {
      return;
    }
    previousStep.current = step;
    previousSource.current = sourceConnectionId;
    previousTarget.current = targetConnectionId;
    previousPhase.current = jobStatus.phase ?? null;
    readyForToasts.current = true;
  }, [bearer, jobStatus.phase, sourceConnectionId, step, targetConnectionId]);

  useEffect(() => {
    if (!readyForToasts.current || previousStep.current === step) {
      previousStep.current = step;
      return;
    }

    if (step === "progress") {
      notify({
        tone: "success",
        title: "Migration launched",
        message: currentMigrationId
          ? `Tracking run ${shortId(currentMigrationId)} in the live dashboard.`
          : "The live dashboard is now tracking the active run.",
      });
    }

    if (step === "report") {
      notify({
        tone: "info",
        title: "Report ready",
        message: "The migration output is available for review.",
      });
    }

    previousStep.current = step;
  }, [currentMigrationId, notify, step]);

  useEffect(() => {
    if (!readyForToasts.current || previousSource.current === sourceConnectionId) {
      previousSource.current = sourceConnectionId;
      return;
    }

    if (sourceConnectionId) {
      notify({
        tone: "info",
        title: "Source workspace selected",
        message: describeConnection(sourceConnections, sourceConnectionId),
      });
    }

    previousSource.current = sourceConnectionId;
  }, [notify, sourceConnectionId, sourceConnections]);

  useEffect(() => {
    if (!readyForToasts.current || previousTarget.current === targetConnectionId) {
      previousTarget.current = targetConnectionId;
      return;
    }

    if (targetConnectionId) {
      notify({
        tone: "info",
        title: "Target workspace selected",
        message: describeConnection(targetConnections, targetConnectionId),
      });
    }

    previousTarget.current = targetConnectionId;
  }, [notify, targetConnectionId, targetConnections]);

  useEffect(() => {
    const phase = jobStatus.phase ?? null;
    if (!readyForToasts.current || previousPhase.current === phase) {
      previousPhase.current = phase;
      return;
    }

    if (phase === "completed") {
      notify({
        tone: "success",
        title: "Migration completed",
        message: "The verify report is being prepared now.",
      });
    } else if (phase === "failed") {
      notify({
        tone: "danger",
        title: "Migration failed",
        message: "Check the event stream for the failing resource and retry when ready.",
      });
    } else if (phase === "cancelled") {
      notify({
        tone: "warning",
        title: "Migration cancelled",
        message: "The current run was stopped before completion.",
      });
    }

    previousPhase.current = phase;
  }, [jobStatus.phase, notify]);

  const currentStep = STEP_MAP[step];
  const activeIndex = STEPS.findIndex((item) => item.key === step);
  const sourceConnection = lookupConnection(sourceConnections, sourceConnectionId);
  const targetConnection = lookupConnection(targetConnections, targetConnectionId);
  const connectedCount = Number(Boolean(sourceConnectionId)) + Number(Boolean(targetConnectionId));
  const livePhase = jobStatus.phase ?? (step === "progress" ? "starting" : "idle");
  const mode = describeMode(dryRun, formatTarget);
  const modeTone = dryRun ? "info" : formatTarget ? "danger" : "success";

  let content = <StepContent step={step} />;

  if (bootError) {
    content = (
      <div className="zd-empty-state">
        <div className="zd-chip zd-chip--danger">
          <b>State</b>
          <span>Setup required</span>
        </div>
        <h3>App boot needs attention</h3>
        <p>
          The Zendesk app did not finish its startup handshake. Review the
          backend or manifest configuration and try again.
        </p>
        <pre className="zd-pre-block">{bootError}</pre>
        <div className="zd-inline-actions" style={{ marginTop: 16 }}>
          <button onClick={() => void boot()} className="zd-button zd-button--primary" type="button">
            Retry boot
          </button>
        </div>
      </div>
    );
  } else if (!bearer) {
    content = (
      <div className="zd-empty-state">
        <div className="zd-orbit-loader" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <h3>Getting things ready</h3>
        <p>
          Starting up the app and checking your workspace. This should only take a moment.
        </p>
      </div>
    );
  }

  return (
    <div className="zd-app-shell">
      <div className="zd-shell-grid">
        <header className="zd-admin-header">
          <div className="zd-admin-branding">
            <div className="zd-brand-mark" aria-hidden="true">
              Z
            </div>
            <div className="zd-admin-copy">
              <div className="zd-admin-kicker">Zendesk workspace migration</div>
              <h1 className="zd-brand-title">Transfer</h1>
              <p className="zd-hero-copy">
                Copy your Zendesk settings from one workspace to another — step by step.
              </p>
            </div>
          </div>

          <div className="zd-topbar-meta">
            <MetaChip label="Mode" value={mode} tone={modeTone} />
            {livePhase !== "idle" ? (
              <MetaChip label="Status" value={livePhase} tone={phaseTone(livePhase)} />
            ) : null}
            <button
              type="button"
              className="zd-sound-toggle"
              aria-pressed={muted}
              title={muted ? "Sound effects muted — click to unmute" : "Sound effects on"}
              onClick={() => {
                const next = !muted;
                setMuted(next);
                setMutedState(next);
              }}
            >
              <span aria-hidden="true">{muted ? "🔇" : "🔊"}</span>
              <span className="zd-sound-toggle-label">{muted ? "Muted" : "Sound"}</span>
            </button>
            <ThemeToggle />
          </div>
        </header>

        <section className="zd-workspace">
          <div className="zd-workspace-head">
            <div className="zd-workspace-copy">
              <div className="zd-section-eyebrow">{currentStep.eyebrow}</div>
              <h2 className="zd-screen-title">{currentStep.title}</h2>
              <p className="zd-screen-copy">{currentStep.description}</p>
            </div>

            <div className="zd-overview-grid">
              <div className="zd-overview-tile">
                <b>Connections</b>
                <strong>{connectedCount}/2 ready</strong>
              </div>
              <div className="zd-overview-tile">
                <b>Phases</b>
                <strong>{selectedPhases.length > 0 ? selectedPhases.join(", ") : "None"}</strong>
              </div>
              <div className="zd-overview-tile">
                <b>From</b>
                <strong>{sourceConnection?.subdomain ?? "—"}</strong>
              </div>
              <div className="zd-overview-tile">
                <b>To</b>
                <strong>{targetConnection?.subdomain ?? "—"}</strong>
              </div>
            </div>
          </div>

          <nav className="zd-step-tabs" aria-label="Transfer steps">
            {STEPS.map((item, index) => {
              const isActive = item.key === step;
              const isDone = index < activeIndex;
              return (
                <button
                  key={item.key}
                  className={`zd-step-tab${isActive ? " is-active" : ""}${isDone ? " is-done" : ""}`}
                  onClick={() => setStep(item.key)}
                  type="button"
                >
                  <span className="zd-step-tab-index">{index + 1}</span>
                  <span className="zd-step-tab-copy">
                    <strong>{item.label}</strong>
                    <small>{item.eyebrow}</small>
                  </span>
                </button>
              );
            })}
          </nav>

          <div className="zd-workspace-body">{content}</div>
        </section>
      </div>
    </div>
  );
}

function StepContent({ step }: { step: WizardStep }) {
  if (step === "preflight") {
    return <PreFlight />;
  }
  if (step === "source-auth") {
    return <SourceAuth />;
  }
  if (step === "choose-phases") {
    return <ChoosePhases />;
  }
  if (step === "preview-confirm") {
    return <PreviewConfirm />;
  }
  if (step === "progress") {
    return <ProgressDashboard />;
  }
  return <ReportRollback />;
}

function MetaChip({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: ChipTone;
}) {
  return (
    <div className={`zd-chip zd-chip--${tone}`}>
      <b>{label}</b>
      <span>{value}</span>
    </div>
  );
}

function lookupConnection(
  list: MaskedConnection[],
  id: string | null,
): MaskedConnection | null {
  if (!id) {
    return null;
  }
  return list.find((connection) => connection.id === id) ?? null;
}

function describeConnection(list: MaskedConnection[], id: string): string {
  const connection = list.find((item) => item.id === id);
  if (!connection) {
    return shortId(id);
  }
  const authLabel = connection.auth_kind === "oauth" ? "OAuth" : "API token";
  return `${connection.subdomain}.zendesk.com via ${authLabel}`;
}

function shortId(value: string): string {
  if (value.length <= 12) {
    return value;
  }
  return `${value.slice(0, 8)}...${value.slice(-4)}`;
}

function describeMode(dryRun: boolean, formatTarget: boolean): string {
  if (dryRun) {
    return "Dry run";
  }
  if (formatTarget) {
    return "Format + write";
  }
  return "Live write";
}

function phaseTone(phase: string): ChipTone {
  if (phase === "completed") {
    return "success";
  }
  if (phase === "failed") {
    return "danger";
  }
  if (phase === "cancelled") {
    return "warning";
  }
  if (phase === "idle") {
    return "ghost";
  }
  return "accent";
}
