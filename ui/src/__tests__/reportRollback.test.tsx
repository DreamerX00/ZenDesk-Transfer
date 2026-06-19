import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReportRollback } from "../steps/ReportRollback";
import { useStore } from "../state/store";

const { getReport } = vi.hoisted(() => ({
  getReport: vi.fn(),
}));

vi.mock("../api/backend", () => ({
  downloadUrl: (path: string) => path,
  getReport,
  listBackups: vi.fn(async () => []),
  listMigrations: vi.fn(async () => []),
  startCleanup: vi.fn(),
  startRestore: vi.fn(),
  startRollback: vi.fn(),
}));

describe("ReportRollback", () => {
  beforeEach(() => {
    useStore.setState({
      currentMigrationId: "mid-123",
      targetConnectionId: "target-1",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    useStore.getState().reset();
  });

  it("retries the current report when the backend is briefly not ready", async () => {
    vi.useFakeTimers();
    getReport
      .mockRejectedValueOnce(new Error("report not found yet"))
      .mockResolvedValueOnce("# Migration Report\n\nReady now");

    await act(async () => {
      render(<ReportRollback />);
      await Promise.resolve();
    });

    expect(getReport).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Waiting for the report to be ready...")).toBeTruthy();

    await act(async () => {
      vi.advanceTimersByTime(1500);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getReport).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Ready now")).toBeTruthy();
  });

  it("shows a clear empty state when there is no active migration id", async () => {
    useStore.setState({ currentMigrationId: null });

    await act(async () => {
      render(<ReportRollback />);
      await Promise.resolve();
    });

    expect(await screen.findByText("No report yet")).toBeTruthy();
    expect(await screen.findByText("Run a transfer first to see the results here.")).toBeTruthy();
  });
});
