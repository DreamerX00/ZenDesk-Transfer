/**
 * ZAF client singleton + helpers for talking to the iframe's host
 * Zendesk runtime.
 *
 * The ZAF SDK is provided at runtime by Zendesk via the global script
 * tag in iframe.html. Outside the iframe (e.g. during Vitest) we
 * fall back to a no-op stub so importing this module doesn't crash.
 */

import type { ZAFClient, ZAFContext, ZAFMetadata } from "../zaf";

let _client: ZAFClient | null = null;

export function getZafClient(): ZAFClient {
  if (_client) return _client;
  if (typeof window !== "undefined" && window.ZAFClient) {
    _client = window.ZAFClient.init();
    return _client;
  }
  // Vitest / non-iframe fallback — every call throws so missing
  // mocks are surfaced loudly rather than silently swallowed.
  const err = (): never => {
    throw new Error("ZAFClient is not available outside the Zendesk iframe");
  };
  return {
    on: err, off: err, get: err, set: err, invoke: err,
    request: err, context: err, metadata: err, trigger: err,
  } as unknown as ZAFClient;
}

export async function getContext(): Promise<ZAFContext> {
  return await getZafClient().context();
}

export async function getMetadata(): Promise<ZAFMetadata> {
  return await getZafClient().metadata();
}

/**
 * Read a backend setting (URL or HMAC secret) from the app manifest
 * parameters. Used at iframe boot to learn where to send /session.
 */
export async function getBackendSettings(): Promise<{
  backendUrl: string;
  backendSecret: string;
}> {
  const meta = await getMetadata();
  const s = meta.settings || {};
  return {
    backendUrl: String(s.backend_url || "").replace(/\/+$/, ""),
    backendSecret: String(s.backend_secret || ""),
  };
}
