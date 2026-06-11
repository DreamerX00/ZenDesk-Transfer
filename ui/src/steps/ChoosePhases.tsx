import { useEffect, useState } from "react";
import { useStore } from "../state/store";
import { btn } from "./PreFlight";

const PHASE_LABELS: Array<[number, string, string]> = [
  [1, "Foundation", "Groups, organizations, ticket forms, and custom fields."],
  [3, "Content", "Help Center categories, sections, articles, segments, and themes."],
  [2, "Business logic", "Macros, triggers, automations, views, SLAs, and webhooks."],
  [5, "Users", "End-users and, if desired, agent identities."],
  [4, "Verify", "Post-migration verification and reporting output."],
];

const SUSPENSION_THRESHOLD = 500;

export function ChoosePhases() {
  const setStep = useStore((s) => s.setStep);
  const selectedPhases = useStore((s) => s.selectedPhases);
  const setSelectedPhases = useStore((s) => s.setSelectedPhases);
  const maxUsers = useStore((s) => s.maxUsers);
  const setMaxUsers = useStore((s) => s.setMaxUsers);
  const usersFrom = useStore((s) => s.usersFrom);
  const setUsersFrom = useStore((s) => s.setUsersFrom);
  const dryRun = useStore((s) => s.dryRun);
  const setDryRun = useStore((s) => s.setDryRun);
  const formatTarget = useStore((s) => s.formatTarget);
  const setFormatTarget = useStore((s) => s.setFormatTarget);
  const [activeTab, setActiveTab] = useState<"scope" | "users" | "options">("scope");

  function toggle(phaseNumber: number) {
    const set = new Set(selectedPhases);
    if (set.has(phaseNumber)) {
      set.delete(phaseNumber);
    } else {
      set.add(phaseNumber);
    }
    setSelectedPhases(Array.from(set).sort());
  }

  const usersSelected = selectedPhases.includes(5);
  const showSuspensionHint =
    usersSelected && (maxUsers === null || maxUsers > SUSPENSION_THRESHOLD);

  useEffect(() => {
    if (!usersSelected && activeTab === "users") {
      setActiveTab("scope");
    }
  }, [activeTab, usersSelected]);

  return (
    <div className="zd-stack">
      <div className="zd-panel zd-panel--raised">
        <div className="zd-panel-header">
          <div>
            <h3>Migration settings</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Use tabs to move between scope, user batching, and run options
              without scrolling through the full screen every time.
            </p>
          </div>
          <div className="zd-chip zd-chip--accent">
            <b>Active</b>
            <span>{selectedPhases.length} phases</span>
          </div>
        </div>

        <div className="zd-section-tabs" style={{ paddingLeft: 0, paddingRight: 0 }}>
          <button
            className={`zd-section-tab${activeTab === "scope" ? " is-active" : ""}`}
            onClick={() => setActiveTab("scope")}
            type="button"
          >
            <strong>Scope</strong>
            <span>Select migration phases</span>
          </button>
          <button
            className={`zd-section-tab${activeTab === "users" ? " is-active" : ""}`}
            onClick={() => setActiveTab("users")}
            type="button"
          >
            <strong>User batch</strong>
            <span>{usersSelected ? "Batching enabled" : "Enable Users phase first"}</span>
          </button>
          <button
            className={`zd-section-tab${activeTab === "options" ? " is-active" : ""}`}
            onClick={() => setActiveTab("options")}
            type="button"
          >
            <strong>Run options</strong>
            <span>Dry run and formatting</span>
          </button>
        </div>

        <div className="zd-tab-panel">
          {activeTab === "scope" ? (
            <>
              <div className="zd-choice-grid">
                {PHASE_LABELS.map(([phaseNumber, label, description]) => {
                  const active = selectedPhases.includes(phaseNumber);
                  return (
                    <label
                      key={phaseNumber}
                      className={`zd-choice-card${active ? " is-active" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={() => toggle(phaseNumber)}
                        style={{ marginTop: 4 }}
                      />
                      <div>
                        <strong>
                          {phaseNumber}. {label}
                        </strong>
                        <p>{description}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
              {usersSelected ? (
                <div className="zd-callout zd-callout--danger" style={{ marginTop: 12 }}>
                  <strong>⚠ User migration selected.</strong> Migrating users is a
                  destructive, high-risk operation that can trigger Zendesk account
                  suspension. Verify the target is correct before proceeding.
                </div>
              ) : null}
            </>
          ) : null}

          {activeTab === "users" ? (
            usersSelected ? (
              <div className="zd-stack">
                <div className="zd-choice-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
                  <label className="zd-field" style={{ marginTop: 0 }}>
                    <span>Max users in this run</span>
                    <input
                      className="zd-input"
                      type="number"
                      min={1}
                      value={maxUsers ?? ""}
                      onChange={(event) => {
                        setMaxUsers(event.target.value ? parseInt(event.target.value, 10) : null);
                      }}
                      placeholder="all"
                    />
                  </label>

                  <label className="zd-field" style={{ marginTop: 0 }}>
                    <span>Start offset</span>
                    <input
                      className="zd-input"
                      type="number"
                      min={0}
                      value={usersFrom}
                      onChange={(event) => {
                        setUsersFrom(parseInt(event.target.value || "0", 10));
                      }}
                    />
                  </label>
                </div>

                {showSuspensionHint ? (
                  <div className="zd-callout zd-callout--warning">
                    <strong>Suspension risk:</strong> migrating more than {SUSPENSION_THRESHOLD} users
                    in one burst can trigger Zendesk anomaly detection. Ask
                    Zendesk to pre-approve the run or lower the batch size
                    before launching.
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="zd-empty-state">
                <h3 style={{ marginTop: 0, fontSize: "1.05rem" }}>Users phase not selected</h3>
                <p style={{ marginBottom: 0 }}>
                  Enable phase 5 in the Scope tab to configure user batching.
                </p>
              </div>
            )
          ) : null}

          {activeTab === "options" ? (
            <div className="zd-stack">
              <label className={`zd-switch-row${dryRun ? " is-active" : ""}`}>
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(event) => setDryRun(event.target.checked)}
                  style={{ marginTop: 4 }}
                />
                <div>
                  <strong>Dry run</strong>
                  <p>Preview the flow without writing data into the target tenant.</p>
                </div>
              </label>

              <label className={`zd-switch-row${formatTarget ? " is-active" : ""}`}>
                <input
                  type="checkbox"
                  checked={formatTarget}
                  onChange={(event) => setFormatTarget(event.target.checked)}
                  style={{ marginTop: 4 }}
                />
                <div>
                  <strong>Format target first</strong>
                  <p>Clear user-created target configuration before recreating it from source.</p>
                </div>
              </label>

              {formatTarget ? (
                <div className="zd-callout zd-callout--danger">
                  <strong>Destructive path enabled:</strong> formatting the target will
                  delete existing target-side configuration before migration writes.
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      <div className="zd-inline-actions">
        <button onClick={() => setStep("source-auth")} style={btn("secondary")} type="button">
          {"<- Back"}
        </button>
        <button
          onClick={() => setStep("preview-confirm")}
          disabled={selectedPhases.length === 0 && !formatTarget}
          style={btn(selectedPhases.length === 0 && !formatTarget ? "disabled" : "primary")}
          type="button"
        >
          {"Continue ->"}
        </button>
      </div>
    </div>
  );
}
