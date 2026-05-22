import { describe, it, expect, beforeEach } from "vitest";
import { useStore } from "../state/store";

describe("wizard store", () => {
  beforeEach(() => {
    useStore.getState().reset();
  });

  it("starts on the preflight step", () => {
    expect(useStore.getState().step).toBe("preflight");
  });

  it("advances through every wizard step", () => {
    const s = useStore.getState();
    const steps = [
      "preflight",
      "source-auth",
      "choose-phases",
      "preview-confirm",
      "progress",
      "report",
    ] as const;
    for (const target of steps) {
      s.setStep(target);
      expect(useStore.getState().step).toBe(target);
    }
  });

  it("defaults to all five phases selected", () => {
    expect(useStore.getState().selectedPhases).toEqual([1, 2, 3, 4, 5]);
  });

  it("persists phase selection changes", () => {
    useStore.getState().setSelectedPhases([1, 5]);
    expect(useStore.getState().selectedPhases).toEqual([1, 5]);
  });

  it("appends events but never grows past 100", () => {
    const append = useStore.getState().appendEvent;
    for (let i = 0; i < 200; i++) {
      append({ ts: "", action: "CREATED", source_id: i });
    }
    const tail = useStore.getState().eventTail;
    expect(tail.length).toBe(100);
    expect(tail[tail.length - 1].source_id).toBe(199);
  });

  it("reset clears persisted state", () => {
    const s = useStore.getState();
    s.setStep("progress");
    s.setMaxUsers(250);
    s.setCurrentMigrationId("abc");
    s.reset();
    const after = useStore.getState();
    expect(after.step).toBe("preflight");
    expect(after.maxUsers).toBeNull();
    expect(after.currentMigrationId).toBeNull();
  });

  it("keeps session state across reset (session is not persisted)", () => {
    useStore.getState().setSession({
      bearer: "tok", subdomain: "acme", userEmail: "a@b.c",
    });
    useStore.getState().reset();
    // reset() only clears the persisted slice; session survives.
    expect(useStore.getState().bearer).toBe("tok");
  });
});
