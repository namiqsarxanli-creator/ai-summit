import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PanelScreen } from "./PanelScreen";

const PANEL = {
  id: 3, title: "Panel 1", kind: "panel", starts_at: "2026-11-13T06:30:00Z", ends_at: "2026-11-13T07:30:00Z", timezone: "Asia/Baku",
  entry_code: `P3.ENTRY.1.${"a".repeat(64)}`, entry_short: "482913",
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

  it("shows only the entry code and its six digits, even in the panel's last minutes", async () => {
    // FACEMARK, 9 Nov 2026: no exit code; the entry scan earns the panel.
    open(PANEL, "2026-11-13T07:25:00Z");
    expect(await screen.findByLabelText("Code to type")).toHaveTextContent("482 913");
    expect(screen.getByText("Entry code")).toBeInTheDocument();
    expect(screen.queryByText(/Exit code/)).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("shows the Grand Final check-in code on the Grand Final screen", async () => {
    open({ ...PANEL, kind: "grand_final" }, "2026-11-13T13:29:00Z");
    expect(await screen.findByLabelText("Code to type")).toHaveTextContent("482 913");
    expect(screen.getByText("Grand Final code")).toBeInTheDocument();
  });
});
