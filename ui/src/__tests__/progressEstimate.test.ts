import { describe, expect, it } from "vitest";
import { computeEstimate, formatDuration } from "../steps/progressEstimate";
import type { LogRecord } from "../types";

const BASE_MS = Date.parse("2026-06-02T12:00:00.000Z");

function at(seconds: number): string {
  return new Date(BASE_MS + (seconds * 1000)).toISOString();
}

function created(resource: string, seconds: number): LogRecord {
  return {
    ts: at(seconds),
    action: "CREATED",
    resource,
    source_id: seconds,
  };
}

describe("computeEstimate", () => {
  it("uses weighted fallback for phases without item counts", () => {
    const estimate = computeEstimate({
      status: {
        phase: "extract",
        started_at: at(0),
        phase_started_at: at(0),
      },
      events: [],
      now: BASE_MS + (15 * 1000),
      selectedPhases: new Set(["extract", "1-foundation", "5-users"]),
    });

    expect(estimate.totalElapsedSec).toBe(15);
    expect(estimate.phaseElapsedSec).toBe(15);
    expect(estimate.itemsPerSec).toBeNull();
    expect(estimate.etaSec).toBeCloseTo(450, 6);
    // Weight-based fraction for the first phase (extract, weight 0.05) of
    // selected {extract, 1-foundation, 5-users}; totalWeight = 0.60.
    // withinPhase = 1 - 1/(1 + 15/20) = 0.428571…
    // doneWeight = 0.05 * 0.428571 = 0.0214286 → /0.60 = 0.0357143.
    const within = 1 - 1 / (1 + 15 / 20);
    expect(estimate.progressFraction).toBeCloseTo((0.05 * within) / 0.6, 6);
  });

  it("reports progressFraction = 1 on completion and null while too noisy", () => {
    const done = computeEstimate({
      status: {
        phase: "completed",
        started_at: at(0),
        phase_started_at: at(0),
        finished_at: at(100),
      },
      events: [],
      now: BASE_MS + (100 * 1000),
    });
    expect(done.progressFraction).toBe(1);

    const tooEarly = computeEstimate({
      status: { phase: "1-foundation", started_at: at(0), phase_started_at: at(0) },
      events: [],
      now: BASE_MS + (3 * 1000), // < MIN_PHASE_ELAPSED_SEC_FOR_ETA
    });
    expect(tooEarly.progressFraction).toBeNull();
  });

  it("progressFraction grows monotonically as elapsed increases", () => {
    const base = {
      status: { phase: "extract", started_at: at(0), phase_started_at: at(0) },
      events: [],
      selectedPhases: new Set(["extract", "1-foundation", "5-users"]),
    };
    const early = computeEstimate({ ...base, now: BASE_MS + 15 * 1000 });
    const later = computeEstimate({ ...base, now: BASE_MS + 60 * 1000 });
    expect(early.progressFraction).not.toBeNull();
    expect(later.progressFraction).not.toBeNull();
    expect(later.progressFraction!).toBeGreaterThan(early.progressFraction!);
  });

  it("waits for enough completed items before showing ETA", () => {
    const estimate = computeEstimate({
      status: {
        phase: "1-foundation",
        started_at: at(0),
        phase_started_at: at(0),
        extracted_groups: "20",
      },
      events: Array.from({ length: 9 }, (_, i) => created("groups", i)),
      now: BASE_MS + (10 * 1000),
      selectedPhases: new Set(["1-foundation"]),
    });

    expect(estimate.etaSec).toBeNull();
    expect(estimate.itemsPerSec).toBeNull();
    expect(estimate.reason).toContain("9/10 items processed");
  });

  it("counts only top-level phase items and ignores nested subresource logs", () => {
    const phaseEvents: LogRecord[] = [];
    for (let i = 0; i < 10; i += 1) {
      phaseEvents.push(created("hc_articles", i));
      phaseEvents.push(created("hc_article_attachment", i + 0.25));
      phaseEvents.push(created("hc_article_labels", i + 0.5));
    }

    const estimate = computeEstimate({
      status: {
        phase: "3-content",
        started_at: at(0),
        phase_started_at: at(0),
        extracted_articles: "12",
      },
      events: phaseEvents,
      now: BASE_MS + (12 * 1000),
      selectedPhases: new Set(["3-content"]),
    });

    expect(estimate.itemsPerSec).toBeCloseTo(10 / 9, 6);
    expect(estimate.etaSec).toBeCloseTo(1.8, 6);
    expect(estimate.reason).toBeNull();
  });
});

describe("formatDuration", () => {
  it("formats short and long durations cleanly", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0.4)).toBe("< 1s");
    expect(formatDuration(61)).toBe("1m 1s");
    expect(formatDuration(3661)).toBe("1h 1m 1s");
  });
});
