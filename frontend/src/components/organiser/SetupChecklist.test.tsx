import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SETUP, SetupChecklist } from "./SetupChecklist";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

const ADMIN_SECTIONS = ["attendees", "staff", "competition", "masterclasses", "playground", "booths", "interests", "points-log", "system", "audit", "otp-deliveries", "demo-otps"];

describe("SetupChecklist", () => {
  it("lists the server's checks, keeps missing ones visibly incomplete and links each to its admin section", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, {
      ready: false,
      checks: [
        { key: "event", status: "ok", detail: "date 2026-11-13, time zone Asia/Baku" },
        { key: "playground_slots", status: "missing", detail: "no Playground slots prepared" },
        { key: "founder_rule", status: "info", detail: "NOT confirmed - no Founder bonus" },
      ],
    })));
    render(<SetupChecklist />);
    expect(await screen.findByText("Not ready: the missing items below still need the organisers' data.")).toBeInTheDocument();
    const list = within(screen.getByRole("list", { name: "Event setup" }));
    const [event, slots, founder] = list.getAllByRole("listitem");
    expect(within(event!).queryByRole("link")).toBeNull(); // done: nothing to open
    expect(within(slots!).getByText(/Missing/)).toBeInTheDocument();
    expect(within(slots!).getByRole("link", { name: "Open in the admin console" })).toHaveAttribute("href", "/admin?section=playground");
    expect(within(founder!).queryByRole("link")).toBeNull(); // an organiser decision, not a setting here
  });

  it("says when it cannot read the checks", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(403, { error: { code: "ADMIN_REQUIRED", message: "", details: {}, correlation_id: "c" } })));
    render(<SetupChecklist />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("links only to sections the admin console has", () => {
    for (const { section } of Object.values(SETUP)) {
      if (section !== null) expect(ADMIN_SECTIONS).toContain(section);
    }
  });
});
