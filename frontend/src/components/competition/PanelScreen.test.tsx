import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PanelScreen, phaseAt } from "./PanelScreen";

const PANEL = {
  id: 3, title: "Panel 1", kind: "panel", starts_at: "2026-11-13T06:20:00Z", ends_at: "2026-11-13T07:30:00Z", timezone: "Asia/Baku",
  entry_code: `P3.ENTRY.1.${"a".repeat(64)}`, exit_code: `P3.EXIT.1.${"b".repeat(64)}`, entry_short: "482913", exit_short: "554911",
};

function open(body: unknown, at: string) {
  vi.useFakeTimers({ toFake: ["Date"], now: new Date(at) });
  window.history.pushState({}, "", `/screen/panel?k=3.1794600000.${"c".repeat(64)}`);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
  render(<PanelScreen />);
}

describe("PanelScreen", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("changes to the exit code by itself when the last 10 minutes begin", () => {
    expect(phaseAt(PANEL, Date.parse("2026-11-13T07:19:59Z"))).toBe("entry");
    expect(phaseAt(PANEL, Date.parse("2026-11-13T07:20:00Z"))).toBe("exit");
  });

  it("shows the entry code and its six digits, and lets the operator hold the other one", async () => {
    open(PANEL, "2026-11-13T06:25:00Z");
    expect(await screen.findByLabelText("Code to type")).toHaveTextContent("482 913");
    fireEvent.click(screen.getByLabelText("Exit code (last 10 minutes)"));
    expect(screen.getByLabelText("Code to type")).toHaveTextContent("554 911");
  });

  it("has no exit choice on the Grand Final screen", async () => {
    open({ ...PANEL, kind: "grand_final" }, "2026-11-13T13:29:00Z");
    expect(await screen.findByLabelText("Code to type")).toHaveTextContent("482 913");
    expect(screen.queryByLabelText("Exit code (last 10 minutes)")).not.toBeInTheDocument();
  });
});
