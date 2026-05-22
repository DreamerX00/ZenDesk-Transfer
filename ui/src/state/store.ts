/**
 * Wizard state store. Persisted across iframe re-mounts via
 * `ZAFClient.set('zd_transfer_state', ...)` so closing and reopening
 * the modal preserves the user's wizard step + selections.
 *
 * Persistence is best-effort: a failed save (network, ZAF unavailable)
 * is silently ignored — the in-memory state is still correct.
 */

import { create } from "zustand";
import type { MaskedConnection, WizardStep, LogRecord } from "../types";
import { getZafClient } from "../api/zaf";

const STATE_KEY = "zd_transfer_state";

interface PersistedSlice {
  step: WizardStep;
  sourceConnectionId: string | null;
  targetConnectionId: string | null;
  selectedPhases: number[];
  maxUsers: number | null;
  usersFrom: number;
  dryRun: boolean;
  formatTarget: boolean;
  currentMigrationId: string | null;
}

interface SessionSlice {
  bearer: string | null;
  subdomain: string | null;
  userEmail: string | null;
}

interface UISlice {
  sourceConnections: MaskedConnection[];
  targetConnections: MaskedConnection[];
  eventTail: LogRecord[];
  /** Status hash from the backend status hash (string-typed by Redis). */
  jobStatus: Record<string, string>;
  /** Top-level boot error, if any. Renders a banner. */
  bootError: string | null;
}

export interface Store extends PersistedSlice, SessionSlice, UISlice {
  setStep(s: WizardStep): void;
  setSourceConnection(id: string | null): void;
  setTargetConnection(id: string | null): void;
  setSelectedPhases(p: number[]): void;
  setMaxUsers(n: number | null): void;
  setUsersFrom(n: number): void;
  setDryRun(b: boolean): void;
  setFormatTarget(b: boolean): void;
  setCurrentMigrationId(id: string | null): void;
  setSession(s: SessionSlice): void;
  setSourceConnections(list: MaskedConnection[]): void;
  setTargetConnections(list: MaskedConnection[]): void;
  appendEvent(rec: LogRecord): void;
  setEventTail(list: LogRecord[]): void;
  setJobStatus(s: Record<string, string>): void;
  setBootError(msg: string | null): void;
  reset(): void;
}

const initialPersisted: PersistedSlice = {
  step: "preflight",
  sourceConnectionId: null,
  targetConnectionId: null,
  selectedPhases: [1, 2, 3, 4, 5],
  maxUsers: null,
  usersFrom: 0,
  dryRun: false,
  formatTarget: false,
  currentMigrationId: null,
};

const initialSession: SessionSlice = {
  bearer: null,
  subdomain: null,
  userEmail: null,
};

const initialUI: UISlice = {
  sourceConnections: [],
  targetConnections: [],
  eventTail: [],
  jobStatus: {},
  bootError: null,
};

export const useStore = create<Store>((set, get) => ({
  ...initialPersisted,
  ...initialSession,
  ...initialUI,

  setStep: (s) => { set({ step: s }); persist(get()); },
  setSourceConnection: (id) => { set({ sourceConnectionId: id }); persist(get()); },
  setTargetConnection: (id) => { set({ targetConnectionId: id }); persist(get()); },
  setSelectedPhases: (p) => { set({ selectedPhases: p }); persist(get()); },
  setMaxUsers: (n) => { set({ maxUsers: n }); persist(get()); },
  setUsersFrom: (n) => { set({ usersFrom: n }); persist(get()); },
  setDryRun: (b) => { set({ dryRun: b }); persist(get()); },
  setFormatTarget: (b) => { set({ formatTarget: b }); persist(get()); },
  setCurrentMigrationId: (id) => { set({ currentMigrationId: id }); persist(get()); },

  setSession: (s) => set(s),
  setSourceConnections: (list) => set({ sourceConnections: list }),
  setTargetConnections: (list) => set({ targetConnections: list }),
  appendEvent: (rec) =>
    set((st) => ({ eventTail: [...st.eventTail.slice(-99), rec] })),
  setEventTail: (list) => set({ eventTail: list }),
  setJobStatus: (s) => set({ jobStatus: s }),
  setBootError: (msg) => set({ bootError: msg }),

  reset: () => {
    set({ ...initialPersisted, ...initialUI });
    persist(get());
  },
}));

/** Best-effort persistence to the ZAF host. Silently no-ops on failure. */
function persist(state: Store): void {
  const slice: PersistedSlice = {
    step: state.step,
    sourceConnectionId: state.sourceConnectionId,
    targetConnectionId: state.targetConnectionId,
    selectedPhases: state.selectedPhases,
    maxUsers: state.maxUsers,
    usersFrom: state.usersFrom,
    dryRun: state.dryRun,
    formatTarget: state.formatTarget,
    currentMigrationId: state.currentMigrationId,
  };
  try {
    void getZafClient().set(STATE_KEY, slice).catch(() => undefined);
  } catch {
    // ZAF not available (e.g. tests) — no-op.
  }
}

/** Load persisted state from the ZAF host. Returns true on success. */
export async function hydrate(): Promise<boolean> {
  try {
    const got = await getZafClient().get<Record<string, PersistedSlice>>(STATE_KEY);
    const slice = got?.[STATE_KEY];
    if (!slice) return false;
    useStore.setState({ ...slice });
    return true;
  } catch {
    return false;
  }
}
