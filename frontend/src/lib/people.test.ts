import { describe, expect, it } from "vitest";

import { scanMessage, type ScanResult } from "./people";

const t = (key: string) => key;
const mutual = (over: Partial<ScanResult>): ScanResult => ({ connected: true, mutual: true, name: "Aysel M.", kind: "match", ...over });

describe("scanMessage", () => {
  it("says a one-sided scan waits for the other person", () => {
    expect(scanMessage({ connected: false, mutual: false, name: "Aysel M.", outcome: "waiting", points: 0 }, t))
      .toBe("Scanned. When they scan your QR too, you are connected.");
  });

  it("shows the points actually added, never the nominal value", () => {
    expect(scanMessage(mutual({ outcome: "awarded", points: 3 }), t)).toBe("Connected. +3 points for you. Their contact is below.");
    for (const outcome of ["same_company", "ineligible", "not_configured", "closed", "limit_reached"]) {
      expect(scanMessage(mutual({ outcome, points: 0 }), t)).not.toMatch(/\+\d/);
    }
  });

  it("names each zero-point reason", () => {
    expect(scanMessage(mutual({ outcome: "same_company", points: 0 }), t)).toContain("No points: you work at the same company.");
    expect(scanMessage(mutual({ outcome: "ineligible", points: 0 }), t)).toContain("No points: you are not part of the competition.");
    expect(scanMessage(mutual({ outcome: "not_configured", points: 0 }), t)).toContain("No points yet: the competition is not set up yet.");
    expect(scanMessage(mutual({ outcome: "closed", points: 0 }), t)).toContain("No points: the competition has closed.");
    expect(scanMessage(mutual({ outcome: "limit_reached", points: 0 }), t)).toContain("No points: your best four connections already count more.");
  });

  it("tells an already-paid connection from one that never earned points", () => {
    expect(scanMessage(mutual({ outcome: "already_paid", points: 3 }), t)).toBe("Already connected. You hold 3 points from this connection.");
    const unpaid = scanMessage(mutual({ outcome: "unpaid", reason: "not_configured", points: 0 }), t);
    expect(unpaid).toContain("This connection has not earned you points.");
    expect(unpaid).toContain("No points yet: the competition is not set up yet.");
    expect(scanMessage(mutual({ outcome: "unpaid", reason: "not_paid", points: 0 }), t)).toContain("An organiser can review it.");
    expect(scanMessage(mutual({ outcome: "replaced", points: 0 }), t)).toContain("only your best four count");
    expect(scanMessage(mutual({ outcome: "reversed", points: 0 }), t)).toContain("An organiser cancelled");
  });
});
