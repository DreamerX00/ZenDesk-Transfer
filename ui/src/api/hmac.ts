/**
 * HMAC-SHA256 over an arbitrary text body, returning lowercase hex.
 * Uses the Web Crypto API (available in every modern browser).
 *
 * The iframe signs the /session envelope with `backend_secret` from
 * the manifest. The backend uses the same secret to verify.
 */

export async function hmacSha256Hex(
  secret: string,
  message: string,
): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  const bytes = new Uint8Array(sig);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
