/**
 * Time-tracker for the live progress dashboard.
 *
 * Design constraints:
 *   1. NEVER invent a number. If we don't have enough signal, return
 *      `null` and let the UI render "Estimating…".
 *   2. Distinguish *elapsed* (always known if started_at is set) from
 *      *ETA* (only known after enough events).
 *   3. Use the events the worker already publishes — don't add new
 *      backend coordination just for a progress bar.
 *
 * One model, two readouts:
 *   - `progressFraction` (the % bar) is the single source of truth:
 *     weighted phase completion, monotonic left→right.
 *   - `etaSec` (time left) is DERIVED from that same fraction and the
 *     real elapsed time: at fraction f after E seconds the average pace
 *     projects total = E / f, so remaining = E · (1 − f) / f. The bar and
 *     the ETA therefore always agree by construction (50 % after 2 min ⇒
 *     2 min left), and the ETA self-corrects to the account's actual
 *     shape instead of trusting a fixed per-phase calibration.
 *
 * Why this is honest:
 *   - elapsed is wall-clock arithmetic on the timestamps the worker
 *     already writes (`started_at`, `phase_started_at`, `finished_at`)
 *   - the fraction only advances on real signal: items_done/items_total
 *     for item phases, and a bounded time-asymptote for non-item phases
 *     (extract / format / verify) that never fakes 100 %
 *   - fraction and ETA are shown together or not at all — we never draw a
 *     bar we can't put an honest "time left" under
 *   - both stay null (UI shows "Estimating…") until a phase has settled
 *     and, for item phases, produced ≥10 completions over ≥5 s, so we
 *     never divide by a warmup-noise fraction
 */

import type { LogRecord } from "../types";

/**
 * Approximate share of total runtime each phase consumes on a
 * representative full migration (all 5 phases + extract). Numbers come
 * from instrumenting a migration of ~10k users / 4k tickets — they are
 * rough but better than treating every phase as equal weight.
 */
export const PHASE_WEIGHTS: Record<string, number> = {
  "extract": 0.05,
  "format-target": 0.05,
  "1-foundation": 0.05,
  "3-content": 0.25,
  "2-business-logic": 0.10,
  "5-users": 0.50,
  "4-verify": 0.05,
};

// Phase order matches server/jobs.run_full_migration (line 216).
export const PHASE_ORDER = [
  "format-target",
  "extract",
  "1-foundation",
  "3-content",
  "2-business-logic",
  "5-users",
  "4-verify",
] as const;

const MIN_ITEMS_FOR_ETA = 10;
const MIN_TIMESPAN_MS_FOR_ETA = 5_000;
/** Don't show a fraction/ETA until at least this many seconds into the phase —
 *  prevents a bogus number immediately after phase transitions. */
const MIN_PHASE_ELAPSED_SEC_FOR_ETA = 10;
/** e-folding time (seconds) for the non-item within-phase creep. The bar
 *  approaches, but never reaches, the phase's weight cap over ~this long. */
const NON_ITEM_PHASE_TIME_CONSTANT_SEC = 20;

export interface Estimate {
  /** Seconds since the job started (always available once running). */
  totalElapsedSec: number | null;
  /** Seconds since the current phase started. */
  phaseElapsedSec: number | null;
  /**
   * Best-effort remaining seconds for the whole job, or null if too noisy.
   * Derived from `progressFraction`: remaining = elapsed · (1 − f) / f, so it
   * is always consistent with the % bar. Non-null iff `progressFraction` is.
   */
  etaSec: number | null;
  /** Completed items/sec observed during the current phase (debug + UI). */
  itemsPerSec: number | null;
  /** Why ETA is null (UI shows this as a soft hint). */
  reason: string | null;
  /**
   * Overall job completion as a fraction in [0, 1], or null when we can't
   * estimate yet. Derived from elapsed vs. projected total time so it grows
   * monotonically left→right as the job runs (unlike elapsed/(elapsed+eta),
   * which stays roughly flat because the ETA scales with elapsed). The UI
   * progress-bar fill should use THIS, not the time ratio.
   */
  progressFraction: number | null;
}

/**
 * Overall completion fraction based on PHASE WEIGHTS, not the time ratio.
 *
 * = (sum of weights of already-finished phases)
 *   + (within-phase fraction × current phase weight)
 *
 * normalized by the total weight of all phases that will run. This grows
 * monotonically left→right as work completes, because the "finished phases"
 * term only ever increases and the within-phase term advances toward its
 * cap. (The old elapsed/(elapsed+eta) ratio stayed flat: a linearly
 * projected ETA cancels the growing elapsed term.)
 *
 * `withinPhaseFraction` is the current phase's own progress in [0, 1]:
 * items_done/items_total for item phases, or a time-vs-projection estimate
 * for non-item phases. It is clamped to <1 while the phase is running.
 */
function fractionFromPhaseWeights(
  phase: string,
  withinPhaseFraction: number,
  selectedPhases?: ReadonlySet<string>,
): number | null {
  const idx = PHASE_ORDER.indexOf(phase as (typeof PHASE_ORDER)[number]);
  if (idx === -1) return null;

  const runs = (ph: string) =>
    !selectedPhases || selectedPhases.size === 0 || selectedPhases.has(ph);

  let totalWeight = 0;
  let doneWeight = 0;
  for (let i = 0; i < PHASE_ORDER.length; i += 1) {
    const ph = PHASE_ORDER[i];
    if (!runs(ph)) continue;
    const w = PHASE_WEIGHTS[ph] ?? 0;
    totalWeight += w;
    if (i < idx) {
      doneWeight += w; // earlier phases are fully done
    } else if (i === idx) {
      doneWeight += w * Math.min(0.999, Math.max(0, withinPhaseFraction));
    }
  }
  if (totalWeight <= 0) return null;
  return Math.min(0.99, Math.max(0, doneWeight / totalWeight));
}

interface Input {
  status: Record<string, string>;
  events: LogRecord[];
  now: number; // ms epoch, injected for testability
  /** Set of phase names the operator asked the worker to run. Empty = all. */
  selectedPhases?: ReadonlySet<string>;
}

export function computeEstimate({
  status,
  events,
  now,
  selectedPhases,
}: Input): Estimate {
  const totalElapsedSec = parseElapsed(status.started_at, status.finished_at, now);
  const phaseElapsedSec = parseElapsed(status.phase_started_at, status.finished_at, now);
  const phase = status.phase || "";

  // Shared "no estimate yet" shape — both fraction and ETA null together.
  const idle: Estimate = {
    totalElapsedSec,
    phaseElapsedSec,
    etaSec: null,
    itemsPerSec: null,
    reason: null,
    progressFraction: null,
  };

  // Terminal states: stop estimating, just report what happened.
  if (phase === "completed") {
    return { ...idle, etaSec: 0, progressFraction: 1 };
  }
  if (phase === "failed" || phase === "cancelled") {
    // Leave the bar where it was (null → UI keeps the last fill / "stopped").
    return idle;
  }

  // Need at least a phase-start timestamp to compute anything useful.
  if (phaseElapsedSec === null || phase === "starting" || !phase) {
    return { ...idle, reason: "Waiting for the first phase to start." };
  }

  // Settling guard: the fraction is too noisy in the first seconds of a
  // phase — hold the bar rather than show a number that immediately jumps.
  if (phaseElapsedSec < MIN_PHASE_ELAPSED_SEC_FOR_ETA) {
    return { ...idle, reason: "Just started — settling in…" };
  }

  // ---- within-phase progress: the one honest signal per phase ---------- //
  const phaseTotal = expectedItemsForPhase(phase, status);
  let withinPhase: number;
  let itemsPerSec: number | null = null;

  if (phaseTotal === null) {
    // Non-item phase (extract / format-target / verify): no item denominator.
    // Bounded asymptotic creep 1 - 1/(1 + elapsed/T): strictly increasing,
    // starts at 0, approaches but never reaches 1 — so the bar advances
    // without ever faking completion.
    withinPhase = 1 - 1 / (1 + phaseElapsedSec / NON_ITEM_PHASE_TIME_CONSTANT_SEC);
  } else {
    // Item phase: items_done / items_total, but only once we have a real
    // throughput baseline (≥MIN_ITEMS completions over ≥MIN_TIMESPAN). Only
    // completions emitted AFTER phase_started_at count, so we don't average
    // across phases or double-count nested work (attachments, labels).
    const phaseStartMs = Date.parse(status.phase_started_at ?? "");
    const progress = getPhaseProgressEvents(events, phaseStartMs, phase);
    if (progress.length < MIN_ITEMS_FOR_ETA) {
      return {
        ...idle,
        reason: `Estimating… (${progress.length}/${MIN_ITEMS_FOR_ETA} items processed)`,
      };
    }
    const firstTs = Date.parse(progress[0].ts);
    const lastTs = Date.parse(progress[progress.length - 1].ts);
    const spanMs = Math.max(lastTs - firstTs, 0);
    if (spanMs < MIN_TIMESPAN_MS_FOR_ETA) {
      return { ...idle, reason: "Estimating… (collecting throughput baseline)" };
    }
    itemsPerSec = progress.length / (spanMs / 1000);
    withinPhase = phaseTotal > 0 ? Math.min(1, progress.length / phaseTotal) : 0;
  }

  // ---- overall completion fraction: the single source of truth --------- //
  const progressFraction = fractionFromPhaseWeights(phase, withinPhase, selectedPhases);
  if (progressFraction === null || progressFraction <= 0) {
    return { ...idle, itemsPerSec, reason: "Estimating…" };
  }

  // ---- ETA derived FROM that fraction (self-correcting, consistent) ---- //
  // remaining = elapsed · (1 − f) / f. This makes "time left" agree with the
  // bar by construction and adapt to the account's real pace, instead of
  // extrapolating one phase's throughput across the others via fixed weights.
  const elapsed = totalElapsedSec ?? phaseElapsedSec;
  const etaSec = Math.max((elapsed * (1 - progressFraction)) / progressFraction, 0);

  return {
    totalElapsedSec,
    phaseElapsedSec,
    etaSec,
    itemsPerSec,
    reason: null,
    progressFraction,
  };
}

function parseElapsed(
  startIso: string | undefined,
  finishedIso: string | undefined,
  now: number,
): number | null {
  if (!startIso) return null;
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return null;
  // If the job already finished, freeze elapsed at the finish line.
  const end = finishedIso ? Date.parse(finishedIso) : now;
  return Math.max((Number.isNaN(end) ? now : end) - start, 0) / 1000;
}

/** What does the current phase intend to process, in items? */
function expectedItemsForPhase(
  phase: string,
  status: Record<string, string>,
): number | null {
  const buckets = PHASE_RESOURCES[phase];
  if (!buckets) return null;
  let total = 0;
  let hasAny = false;
  for (const key of buckets) {
    const v = Number(status[`extracted_${key}`]);
    if (!Number.isFinite(v) || v < 0) continue;
    total += v;
    hasAny = true;
  }
  return hasAny ? total : null;
}

/**
 * Map phase → which extracted_<resource> counters add up to its work.
 * Mirrors what each phase's `import_resource()` calls actually create:
 *   - phase 1: src/phases/phase1_foundation.py
 *   - phase 2: src/phases/phase2_business_logic.py
 *   - phase 3: src/phases/phase3_content.py
 *   - phase 5: src/phases/phase5_users.py
 * Keep this in sync when a phase gains a new resource.
 */
const PHASE_RESOURCES: Record<string, readonly string[]> = {
  "1-foundation": [
    "groups",
    "brands",
    "ticket_fields",
    "user_fields",
    "organization_fields",
    "custom_roles",
    "ticket_forms",
    "organizations",
  ],
  "2-business-logic": [
    "views",
    "triggers",
    "automations",
    "macros",
    "sla_policies",
    "group_sla_policies",
    "schedules",
    "routing_attributes",
    "dynamic_content_items",
    "webhooks",
  ],
  "3-content": ["categories", "sections", "articles", "user_segments", "themes"],
  "5-users": ["users"],
  // "format-target", "extract", "4-verify" have no item denominator.
};

/** A CREATED/SKIPPED/FAILED event counts as "one item processed". */
function getPhaseProgressEvents(
  events: LogRecord[],
  phaseStartMs: number,
  phase: string,
): LogRecord[] {
  const resources = PHASE_PROGRESS_RESOURCES[phase];
  if (!resources) return [];
  const allowed = new Set(resources);
  return events.filter((event) => {
    const t = Date.parse(event.ts);
    if (Number.isNaN(t) || t < phaseStartMs) return false;
    if (
      event.action !== "CREATED" &&
      event.action !== "SKIPPED" &&
      event.action !== "FAILED"
    ) {
      return false;
    }
    return typeof event.resource === "string" && allowed.has(event.resource);
  });
}

/** Map phase → resource keys that count as "one processed item". */
const PHASE_PROGRESS_RESOURCES: Record<string, readonly string[]> = {
  "1-foundation": [
    "groups",
    "brands",
    "ticket_fields",
    "user_fields",
    "organization_fields",
    "custom_roles",
    "ticket_forms",
    "organizations",
  ],
  "2-business-logic": [
    "views",
    "triggers",
    "automations",
    "macros",
    "sla_policies",
    "group_sla_policies",
    "schedules",
    "routing_attributes",
    "dynamic_content_items",
    "webhooks",
  ],
  "3-content": [
    "hc_categories",
    "hc_sections",
    "hc_articles",
    "hc_user_segments",
    "themes",
  ],
  "5-users": ["users"],
};

/** Format a number of seconds as "Xh Ym Zs" / "Ym Zs" / "Xs". */
export function formatDuration(secs: number | null): string {
  if (secs === null || !Number.isFinite(secs)) return "—";
  if (secs < 1) return "< 1s";
  const total = Math.round(secs);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
