import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SystemStatus, type SystemStatusReport } from "./SystemStatus";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

const NOW = 1_800_000_000;
const report = (over: Partial<SystemStatusReport> = {}): SystemStatusReport => ({
  status: "warn",
  checks: [
    { name: "database", level: "ok", detail: "reachable" },
    { name: "outbox-backlog", level: "crit", detail: "oldest pending 240 s" },
  ],
  admission: { limit: 12, inside: 3, waiting: 0, turned_away: 41 },
  errors: {
    started_at: NOW - 7200,
    now: NOW,
    total_since_start: 2,
    sections: {
      masterclasses: { last_15_min: 2, last_hour: 2, last_day: 2, last_at: NOW - 60, codes: { SERVER_BUSY: 2 } },
    },
    recent: [{ at: NOW - 60, section: "masterclasses", method: "PUT", path: "/api/attendees/me/masterclasses/{id}", status: 503, code: "SERVER_BUSY" }],
  },
  ...over,
});

describe("SystemStatus", () => {
  it("says which part is not working and where the server failed, in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, report())));
    render(<SystemStatus />);
    expect(await screen.findByLabelText("Overall state")).toHaveTextContent("Warning");
    const parts = within(screen.getByRole("region", { name: "Parts of the system" }));
    expect(parts.getByText("Database")).toBeInTheDocument();
    expect(parts.getByText("Sign-in code emails: queue")).toBeInTheDocument();
    expect(parts.getByText("Not working")).toBeInTheDocument();
    expect(parts.getByText(/turned away since start 41/)).toBeInTheDocument();
    const errors = within(screen.getByRole("region", { name: "Server errors by section" }));
    expect(errors.getByText("Masterclasses")).toBeInTheDocument();
    expect(errors.getByText("Server busy (too many requests at once) × 2")).toBeInTheDocument();
    expect(screen.getByText(/PUT \/api\/attendees\/me\/masterclasses\/\{id\}/)).toBeInTheDocument();
  });

  it("says so when there has been no server error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, report({
      status: "ok",
      checks: [{ name: "database", level: "ok", detail: "reachable" }],
      errors: { started_at: NOW - 60, now: NOW, total_since_start: 0, sections: {}, recent: [] },
    }))));
    render(<SystemStatus />);
    expect(await screen.findByText("No server errors since the server started.")).toBeInTheDocument();
    expect(screen.getByLabelText("Overall state")).toHaveTextContent("Working");
    expect(screen.queryByRole("region", { name: "Latest server errors" })).toBeNull();
  });

  it("is refused to anyone but an administrator, and says so", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(403, { error: { code: "STAFF_NOT_AUTHORIZED", message: "x" } })));
    render(<SystemStatus />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
