/**
 * Iframe boot sequence — runs once when App mounts.
 *
 * Steps:
 *   1. Read backend_url + backend_secret from manifest parameters.
 *   2. Fetch ZAFClient.context() — gets subdomain + agent identity.
 *   3. HMAC-sign the envelope and POST /api/v1/session.
 *   4. Hydrate the wizard state from ZAFClient.set/get persistence.
 *
 * Failures at any step set bootError on the store; the UI renders a
 * banner with a retry button. We never throw uncaught — the iframe
 * staying blank would be the worst outcome.
 */

import { getBackendSettings, getContext } from "./api/zaf";
import { hmacSha256Hex } from "./api/hmac";
import {
  postSession,
  postStandaloneSession,
  setBackendUrl,
} from "./api/backend";
import { hydrate, useStore } from "./state/store";

/**
 * Decide whether the bundle is being served standalone (directly from
 * the FastAPI backend) or wrapped by the Zendesk iframe.
 *
 * Two signals must agree:
 *   - The ZAFClient global is present (the SDK script tag loaded).
 *   - The page is actually inside an iframe (window.parent !== window).
 *
 * Loaded but top-level → standalone (operator opened the bundle in a
 * regular tab). Top-level + no ZAFClient → also standalone. Only when
 * both signals say "iframe" do we treat ourselves as a Zendesk app.
 */
function looksLikeStandalone(): boolean {
  if (typeof window === "undefined") return true;
  const sdkLoaded = typeof window.ZAFClient !== "undefined";
  const inIframe = window.parent !== window;
  return !(sdkLoaded && inIframe);
}

export async function boot(): Promise<void> {
  const setBootError = useStore.getState().setBootError;
  setBootError(null);

  if (looksLikeStandalone()) {
    await bootStandalone();
    return;
  }
  await bootInIframe();
}

async function bootInIframe(): Promise<void> {
  const setBootError = useStore.getState().setBootError;
  try {
    const { backendUrl, backendSecret } = await getBackendSettings();
    if (!backendUrl) {
      setBootError(
        "Backend URL is not configured. Open the app settings in " +
          "Zendesk admin and set backend_url.",
      );
      return;
    }
    if (!backendSecret) {
      setBootError(
        "Backend HMAC secret is not configured. Set backend_secret " +
          "in the app settings — it must match ZDX_HMAC_SECRET on the backend.",
      );
      return;
    }
    setBackendUrl(backendUrl);

    const ctx = await getContext();
    const envelope = {
      subdomain: ctx.account.subdomain,
      user: { id: ctx.currentUser.id, email: ctx.currentUser.email },
      ts: Math.floor(Date.now() / 1000),
    };
    const body = JSON.stringify(envelope);
    const sig = await hmacSha256Hex(backendSecret, body);

    const sess = await postSession(body, sig);
    useStore.getState().setSession({
      bearer: sess.token,
      subdomain: sess.subdomain,
      userEmail: sess.user_email,
    });
    await hydrate();
  } catch (exc) {
    const msg =
      exc instanceof Error ? exc.message : "Unknown error during boot";
    setBootError(`Boot failed: ${msg}`);
  }
}

async function bootStandalone(): Promise<void> {
  const setBootError = useStore.getState().setBootError;
  try {
    // When served from the backend, /api/v1/* is on the same origin,
    // so we can derive the base URL from the document location.
    const origin = window.location.origin;
    setBackendUrl(origin);

    const sess = await postStandaloneSession();
    useStore.getState().setSession({
      bearer: sess.token,
      subdomain: sess.subdomain,
      userEmail: sess.user_email,
    });
    await hydrate();
  } catch (exc) {
    const msg =
      exc instanceof Error ? exc.message : "Unknown error during boot";
    setBootError(
      `Standalone boot failed: ${msg}. Make sure the backend is running ` +
        `with ZDX_STANDALONE_MODE=1, or open this app through Zendesk.`,
    );
  }
}
