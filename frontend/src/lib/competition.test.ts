import { describe, expect, it } from "vitest";

import { isPanelCode } from "./competition";

describe("isPanelCode", () => {
  it("takes the screen's QR text and the six digits typed by hand", () => {
    expect(isPanelCode(`P1.ENTRY.1.${"a".repeat(64)}`)).toBe(true);
    expect(isPanelCode("482913")).toBe(true);
    expect(isPanelCode(" 482 913 ")).toBe(true);
    expect(isPanelCode("482-913")).toBe(true);
    expect(isPanelCode("48291")).toBe(false);
    expect(isPanelCode("4829134")).toBe(false);
    expect(isPanelCode("https://example.com")).toBe(false);
  });
});

describe("cardShortCode", () => {
  it("takes the six characters under a card, typed loosely, and nothing else", async () => {
    const { cardShortCode } = await import("./competition");
    expect(cardShortCode("k7m-4qx")).toBe("K7M4QX");
    expect(cardShortCode(" K7M 4QX ")).toBe("K7M4QX");
    expect(cardShortCode("K7M4Q")).toBeNull();
    expect(cardShortCode("K0M4QX")).toBeNull(); // 0, O, 1, I and L are never used
    expect(cardShortCode("KLM4QX")).toBeNull();
  });
});
