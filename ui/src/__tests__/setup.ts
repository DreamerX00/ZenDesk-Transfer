/**
 * Vitest setup. Runs before every test file. We stub the ZAF SDK and
 * the Web Crypto API so the modules under test don't crash on import.
 */

import "@testing-library/react";

// Stub ZAFClient — every call returns a resolved promise with a sane default.
type Stub = {
  get: ReturnType<typeof globalThis.Object.assign> & ((...a: unknown[]) => Promise<unknown>);
  [k: string]: unknown;
};

const noop = async () => undefined;

(globalThis as unknown as { ZAFClient?: unknown }).ZAFClient = {
  init(): Stub {
    return {
      on: noop, off: noop, invoke: noop, request: noop, trigger: noop,
      get: async (key: string | string[]) => {
        const k = Array.isArray(key) ? key[0] : key;
        return { [k]: undefined };
      },
      set: noop,
      context: async () => ({
        product: "support",
        account: { subdomain: "test" },
        currentUser: { id: 1, email: "a@b.c", name: "Test" },
        location: "nav_bar",
        instanceGuid: "x",
      }),
      metadata: async () => ({
        installationId: 1, appId: 1, name: "test", version: "0.0.0",
        settings: { backend_url: "http://localhost:8080", backend_secret: "secret" },
      }),
    };
  },
};

(globalThis as unknown as { window?: typeof globalThis }).window = globalThis;
