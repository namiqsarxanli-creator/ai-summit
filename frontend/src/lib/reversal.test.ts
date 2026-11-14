import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "./errors";
import { hasTranslation, translate } from "./i18n";
import {
  REASON_MAX, REVERSAL_TEXTS, canReverse, newIdempotencyKey, normalizeReason, reasonProblem, reverseEntry,
} from "./reversal";

afterEach(() => { vi.unstubAllGlobals(); });

describe("reversal rules", () => {
  it("only an award that nothing has reversed can be reversed", () => {
    expect(canReverse({ id: 1, entry_type: "AWARD", reversed_by_entry_id: null })).toBe(true);
    expect(canReverse({ id: 1, entry_type: "AWARD", reversed_by_entry_id: 5 })).toBe(false);
    expect(canReverse({ id: 2, entry_type: "REVERSAL", reversed_by_entry_id: null })).toBe(false);
    expect(canReverse({ entry_type: "AWARD", reversed_by_entry_id: null })).toBe(false);
    expect(canReverse({ id: 1, entry_type: "AWARD" })).toBe(false); // an answer from an older server: not offered
  });

  it("tidies a reason the way the server does and checks its length", () => {
    expect(normalizeReason("  a \n b\t\tc ")).toBe("a b c");
    expect(reasonProblem("")).not.toBe("");
    expect(reasonProblem("ab")).not.toBe("");
    expect(reasonProblem("abc")).toBe("");
    expect(reasonProblem("é".repeat(REASON_MAX))).toBe("");
    expect(reasonProblem("x".repeat(REASON_MAX + 1))).not.toBe("");
    expect(reasonProblem("tidy   ".repeat(60))).toBe(""); // 420 typed, 299 after the whitespace is collapsed
    expect(reasonProblem("tidy   ".repeat(61))).not.toBe(""); // 303 after collapsing
    expect(reasonProblem("bell \u0007 inside")).not.toBe("");
  });

  it("posts the reason and the key to the entry's own URL - nothing about amounts or people", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ reversal_id: 1 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await reverseEntry(41, "Wrong person", "11111111-2222-4333-8444-555555555555");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/admin/points-log/41/reverse");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ reason: "Wrong person", idempotency_key: "11111111-2222-4333-8444-555555555555" });
  });

  it("makes v4-shaped idempotency keys, with or without crypto.randomUUID (a plain-HTTP LAN page has none)", () => {
    const shape = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(newIdempotencyKey()).toMatch(shape);
    vi.stubGlobal("crypto", { getRandomValues: (a: Uint8Array) => { a.fill(171); return a; } });
    const key = newIdempotencyKey();
    expect(key).toMatch(shape);
    expect(newIdempotencyKey()).toBe(key); // (the stub is constant: this is only about the shape)
  });
});

describe("reversal texts", () => {
  it("every text of the screen, and every error it can show, has an Azerbaijani and a Russian translation", () => {
    const errors = ["POINTS_ENTRY_NOT_FOUND", "POINTS_NOT_REVERSIBLE", "POINTS_ALREADY_REVERSED"].map(messageFor);
    const missing = [...new Set([...REVERSAL_TEXTS, ...errors])].filter((text) => !hasTranslation(text));
    expect(missing).toEqual([]);
    for (const text of ["Reverse points", "Back to the ledger", "What happens"]) {
      expect(translate(text, "az")).not.toBe(text);
      expect(translate(text, "ru")).not.toBe(text);
    }
  });
});
