import { useStore } from "../state/store";

const PHASES: Array<{
  number: number;
  label: string;
  description: string;
  warning?: string;
}> = [
  {
    number: 1,
    label: "Settings & Structure",
    description: "Groups, ticket forms, custom fields, organizations, and brands.",
  },
  {
    number: 2,
    label: "Rules & Automation",
    description: "Triggers, macros, automations, views, SLA policies, schedules, and webhooks.",
  },
  {
    number: 3,
    label: "Help Center",
    description: "Categories, sections, articles, user segments, and themes.",
  },
  {
    number: 4,
    label: "Verify",
    description: "Post-migration check that compares source and target to make sure everything matches.",
  },
  {
    number: 5,
    label: "Users & Agents",
    description: "End-user and agent accounts. This is a high-risk operation.",
    warning: "Migrating users can trigger Zendesk's account suspension detection. Use with caution.",
  },
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
  const showSuspensionHint = usersSelected && (maxUsers === null || maxUsers > SUSPENSION_THRESHOLD);

  return (
    <div className="zd-stack">
      <div className="zd-card zd-card--raised">
        <div className="zd-panel-header">
          <div>
            <h3>What to copy</h3>
            <p className="zd-body-copy" style={{ margin: "6px 0 0" }}>
              Turn on the items you want to transfer. You can always run again to copy more later.
            </p>
          </div>
          <span className="zd-chip zd-chip--brand">
            <b>Selected</b>
            <span>{selectedPhases.length} of {PHASES.length}</span>
          </span>
        </div>

        {/* Phase selection cards */}
        <div className="zd-choice-grid">
          {PHASES.map((phase) => {
            const active = selectedPhases.includes(phase.number);
            return (
              <label
                key={phase.number}
                className={`zd-choice-card${active ? " is-active" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={active}
                  onChange={() => toggle(phase.number)}
                  style={{ marginTop: 3 }}
                />
                <div>
                  <strong>{phase.number}. {phase.label}</strong>
                  <p>{phase.description}</p>
                  {active && phase.warning ? (
                    <p style={{ marginTop: 8, color: "var(--danger)", fontSize: 11 }}>
                      {phase.warning}
                    </p>
                  ) : null}
                </div>
              </label>
            );
          })}
        </div>
      </div>

      {/* User batching — visible only when Users phase is selected */}
      {usersSelected ? (
        <div className="zd-card">
          <div className="zd-panel-header">
            <h3>User batch settings</h3>
          </div>
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: 12,
          }}>
            <label className="zd-field" style={{ marginTop: 0 }}>
              <span>Maximum users to copy</span>
              <input
                className="zd-input"
                type="number"
                min={1}
                value={maxUsers ?? ""}
                onChange={(event) => {
                  setMaxUsers(event.target.value ? parseInt(event.target.value, 10) : null);
                }}
                placeholder="Copy all"
              />
            </label>
            <label className="zd-field" style={{ marginTop: 0 }}>
              <span>Start from user number</span>
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
          <div style={{ fontSize: 12, color: "var(--text-soft)", marginTop: 8 }}>
            Leave "Maximum users" empty to copy all users. Use "Start from" to skip the first N users.
          </div>
          {showSuspensionHint ? (
            <div className="zd-callout zd-callout--warning" style={{ marginTop: 12 }}>
              <strong>Heads up:</strong> Copying more than {SUSPENSION_THRESHOLD} users at once can look suspicious to Zendesk's automated systems.
              Consider splitting into smaller batches or contacting Zendesk support first.
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Run options — always visible */}
      <div className="zd-card">
        <div className="zd-panel-header">
          <h3>Run options</h3>
        </div>
        <div className="zd-stack" style={{ gap: 10 }}>
          <label className={`zd-switch-row${dryRun ? " is-active" : ""}`}>
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(event) => setDryRun(event.target.checked)}
              style={{ marginTop: 3 }}
            />
            <div>
              <strong>Dry run (preview only)</strong>
              <p>Check the process without actually changing anything in the target account.</p>
            </div>
          </label>

          <label className={`zd-switch-row${formatTarget ? " is-active" : ""}`}>
            <input
              type="checkbox"
              checked={formatTarget}
              onChange={(event) => setFormatTarget(event.target.checked)}
              style={{ marginTop: 3 }}
            />
            <div>
              <strong>Clear target first</strong>
              <p>Remove existing settings in the target account before copying new ones.</p>
            </div>
          </label>

          {formatTarget ? (
            <div className="zd-callout zd-callout--danger">
              <strong>This will delete existing settings.</strong> The target account's current
              configuration will be removed before the copy begins. A backup is taken automatically.
            </div>
          ) : null}
        </div>
      </div>

      <div className="zd-inline-actions">
        <button onClick={() => setStep("source-auth")} className="zd-button zd-button--secondary" type="button">
          Back
        </button>
        <button
          onClick={() => setStep("preview-confirm")}
          disabled={selectedPhases.length === 0 && !formatTarget}
          className="zd-button zd-button--primary"
          type="button"
        >
          {selectedPhases.length > 0 || formatTarget ? "Review & start" : "Select at least one item"}
        </button>
      </div>
    </div>
  );
}
