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
        phase_started_at: at(5),
      },
      events: [],
      now: BASE_MS + (10 * 1000),
      selectedPhases: new Set(["extract", "1-foundation", "5-users"]),
    });

    expect(estimate.totalElapsedSec).toBe(10);
    expect(estimate.phaseElapsedSec).toBe(5);
    expect(estimate.itemsPerSec).toBeNull();
    expect(estimate.reason).toContain("non-item work");
    expect(estimate.etaSec).toBeCloseTo(355, 6);
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
