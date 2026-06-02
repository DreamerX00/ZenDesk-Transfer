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
 * Why this is honest:
 *   - elapsed is wall-clock arithmetic on the timestamps the worker
 *     already writes (`started_at`, `phase_started_at`, `finished_at`)
 *   - in-phase ETA is `remaining / observed_items_per_sec`, computed
 *     only when we have ≥10 item-completion events spanning ≥5 s —
 *     below that, throughput is dominated by warmup noise and
 *     produces wildly swinging numbers
 *   - remaining-phases ETA is scaled by historical phase weights
 *     measured on a representative migration; we apply them as a
 *     multiplier on the current phase's pace, so unrealistic source
 *     sizes scale honestly rather than reporting a fixed minutes count
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

export interface Estimate {
  /** Seconds since the job started (always available once running). */
  totalElapsedSec: number | null;
  /** Seconds since the current phase started. */
  phaseElapsedSec: number | null;
  /** Best-effort remaining seconds for the whole job, or null if too noisy. */
  etaSec: number | null;
  /** Completed items/sec observed during the current phase (debug + UI). */
  itemsPerSec: number | null;
  /** Why ETA is null (UI shows this as a soft hint). */
  reason: string | null;
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

  // Terminal states: stop estimating, just report what happened.
  if (phase === "completed" || phase === "failed" || phase === "cancelled") {
    return {
      totalElapsedSec,
      phaseElapsedSec,
      etaSec: 0,
      itemsPerSec: null,
      reason: null,
    };
  }

  // Need at least a phase-start timestamp to compute anything useful.
  if (phaseElapsedSec === null || phase === "starting" || !phase) {
    return {
      totalElapsedSec,
      phaseElapsedSec,
      etaSec: null,
      itemsPerSec: null,
      reason: "Waiting for the first phase to start.",
    };
  }

  // How many items does this phase intend to process? extracted_<r>
  // gives us per-resource totals; sum the ones relevant to the phase.
  const phaseTotal = expectedItemsForPhase(phase, status);

  // Non-item phases such as extract / format-target / verify have no
  // honest per-item denominator. Fall back to historical phase weights
  // immediately instead of waiting for noisy note-level events.
  if (phaseTotal === null) {
    const phaseWeight = PHASE_WEIGHTS[phase] ?? 0.05;
    const expectedPhaseSec = Math.max(phaseWeight * 600, 15);
    const phaseRemainingSec = Math.max(expectedPhaseSec - phaseElapsedSec, 0);
    const phasePerWeightSec = (phaseElapsedSec + phaseRemainingSec) / phaseWeight;
    const remainingWeight = sumRemainingPhaseWeights(phase, selectedPhases);

    return {
      totalElapsedSec,
      phaseElapsedSec,
      etaSec: Math.max(phaseRemainingSec + (phasePerWeightSec * remainingWeight), 0),
      itemsPerSec: null,
      reason: "Estimated from typical phase duration for non-item work.",
    };
  }

  // Throughput inside the current phase — only completed main-resource
  // events emitted AFTER phase_started_at count, otherwise we'd average
  // across phases or double-count nested work like attachments.
  const phaseStartMs = Date.parse(status.phase_started_at ?? "");
  const phaseProgressEvents = getPhaseProgressEvents(events, phaseStartMs, phase);
  if (phaseProgressEvents.length < MIN_ITEMS_FOR_ETA) {
    return {
      totalElapsedSec,
      phaseElapsedSec,
      etaSec: null,
      itemsPerSec: null,
      reason: `Estimating… (${phaseProgressEvents.length}/${MIN_ITEMS_FOR_ETA} items processed)`,
    };
  }

  const firstTs = Date.parse(phaseProgressEvents[0].ts);
  const lastTs = Date.parse(phaseProgressEvents[phaseProgressEvents.length - 1].ts);
  const spanMs = Math.max(lastTs - firstTs, 0);
  if (spanMs < MIN_TIMESPAN_MS_FOR_ETA) {
    return {
      totalElapsedSec,
      phaseElapsedSec,
      etaSec: null,
      itemsPerSec: null,
      reason: "Estimating… (collecting throughput baseline)",
    };
  }
  const itemsPerSec = phaseProgressEvents.length / (spanMs / 1000);
  const phaseDone = phaseProgressEvents.length;
  const phaseRemainingSec = phaseTotal > phaseDone ? (phaseTotal - phaseDone) / itemsPerSec : 0;

  // Remaining phases: scale the current phase's elapsed by the weight
  // ratio. If we know phase-A took 60 s at weight 0.10 and phase-B is
  // weight 0.50, phase-B will take ~300 s.
  const phaseWeight = PHASE_WEIGHTS[phase] ?? 0.1;
  const phasePerWeightSec = ((phaseElapsedSec ?? 0) + phaseRemainingSec) / phaseWeight;

  const remainingWeight = sumRemainingPhaseWeights(phase, selectedPhases);
  const otherPhasesSec = phasePerWeightSec * remainingWeight;

  return {
    totalElapsedSec,
    phaseElapsedSec,
    etaSec: Math.max(phaseRemainingSec + otherPhasesSec, 0),
    itemsPerSec,
    reason: null,
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
  "3-content": ["categories", "sections", "articles", "user_segments"],
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
  ],
  "5-users": ["users"],
};

function sumRemainingPhaseWeights(
  currentPhase: string,
  selectedPhases?: ReadonlySet<string>,
): number {
  const idx = PHASE_ORDER.indexOf(currentPhase as (typeof PHASE_ORDER)[number]);
  if (idx === -1) return 0;
  let sum = 0;
  for (let i = idx + 1; i < PHASE_ORDER.length; i += 1) {
    const ph = PHASE_ORDER[i];
    if (selectedPhases && selectedPhases.size > 0 && !selectedPhases.has(ph)) continue;
    sum += PHASE_WEIGHTS[ph] ?? 0;
  }
  return sum;
}

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
