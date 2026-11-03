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
