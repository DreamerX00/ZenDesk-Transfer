import { describe, it, expect } from "vitest";
import { hmacSha256Hex } from "../api/hmac";

describe("hmacSha256Hex", () => {
  it("matches RFC 4231 test vector #2", async () => {
    // HMAC-SHA256("Jefe", "what do ya want for nothing?")
    // = 5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843
    const got = await hmacSha256Hex("Jefe", "what do ya want for nothing?");
    expect(got).toBe(
      "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
    );
  });

  it("is deterministic for same inputs", async () => {
    const a = await hmacSha256Hex("secret", "hello");
    const b = await hmacSha256Hex("secret", "hello");
    expect(a).toBe(b);
  });

  it("changes when secret changes", async () => {
    const a = await hmacSha256Hex("secret-a", "hello");
    const b = await hmacSha256Hex("secret-b", "hello");
    expect(a).not.toBe(b);
  });
});
