import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OrganiserPanel } from "./OrganiserPanel";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const failure = (status: number, code: string) =>
  json(status, { error: { code, message: "", details: {}, correlation_id: "c" } });

afterEach(() => vi.unstubAllGlobals());

const GM = {
  title: "FACEMARK — Organiser Panel",
  full_name: "Leyla Game",
  role: "VOLUNTEER",
  game_master: true,
  sections: { overview: true, scanning: true, qmeter: true, corrections: false, final: true, screens: true },
  scanning_jobs: [],
  panel_codes: false,
};
const ADMIN = {
  ...GM,
  full_name: "Organiser Admin",
  role: "ADMIN",
  game_master: false,
  sections: { overview: true, scanning: true, qmeter: false, corrections: true, final: true, screens: true },
  panel_codes: true,
};
const LIVE = {
  state: "not_configured",
  missing: ["close"],
  server_time: "2026-11-13T08:00:00Z",
  timezone: "Asia/Baku",
  closes_at: null,
  top: [],
  recent: [],
  photos: [],
};
const VERSIONS = { closes_at: null, timezone: "Asia/Baku", pending_offline_scans: 2, versions: [] };

function server(workspace: () => Response) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    if (url === "/api/staff/organiser") return workspace();
    if (url === "/api/screen/live") return json(200, LIVE);
    if (url === "/api/staff/final") return json(200, VERSIONS);
    if (url === "/api/panels") return json(200, [{ id: 3, title: "Panel 1", kind: "panel", starts_at: "2026-11-13T06:00:00Z", ends_at: "2026-11-13T07:30:00Z", timezone: "Asia/Baku" }]);
    if (url === "/api/admin/panel-screen-links") return json(200, [{ id: 3, title: "Panel 1", kind: "panel", starts_at: "2026-11-13T06:00:00Z", timezone: "Asia/Baku", key: "3.1763064000.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }]);
    if (url === "/api/staff/me") return json(200, { full_name: "Leyla Game", role: "VOLUNTEER", booth_name: null, queue_owner: "a".repeat(64), dataset_generation: 1 });
    if (url === "/api/staff/me/scopes") return json(200, { scopes: [{ scope: "GAME_MASTER", target_id: null }] });
    if (url === "/api/masterclasses") return json(200, { server_time: "x", events: [] });
    if (url.startsWith("/api/staff/points-log")) return json(200, { items: [], next_cursor: null, limit: 50 });
    if (url.startsWith("/api/staff/connections/unpaid")) return json(200, { items: [], counts: {}, applicable: "eligible" });
    if (url === "/api/admin/readiness") return json(200, { ready: false, checks: [{ key: "competition_close", status: "missing", detail: "NOT SET - nothing can be earned" }] });
    return json(200, { timezone: "Asia/Baku", sessions: [] });
  }));
  return calls;
}

const nav = () => within(screen.getByRole("navigation", { name: "Organiser sections" }));

describe("OrganiserPanel", () => {
  it("asks a signed-out visitor to sign in with a staff account", async () => {
    server(() => failure(401, "UNAUTHENTICATED"));
    render(<OrganiserPanel />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking your session…");
    expect(await screen.findByRole("link", { name: "FACEMARK sign in" })).toHaveAttribute("href", "/facemark/login");
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("refuses booth staff, volunteers and attendees without showing any organiser data", async () => {
    const calls = server(() => failure(403, "STAFF_NOT_AUTHORIZED"));
    render(<OrganiserPanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This panel is for FACEMARK organisers");
    expect(screen.getByRole("link", { name: "Staff page" })).toHaveAttribute("href", "/staff");
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(calls).toEqual(["/api/staff/organiser"]); // nothing else is read
  });

  it("says so when the server cannot answer", async () => {
    server(() => failure(503, "MAINTENANCE"));
    render(<OrganiserPanel />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("shows a Game Master only the sections the server allows, with a real overview", async () => {
    server(() => json(200, GM));
    render(<OrganiserPanel />);
    await screen.findByRole("navigation", { name: "Organiser sections" });
    expect(nav().getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Overview", "Scanning", "Qmeter", "Final results", "Venue screens",
    ]);
    expect(nav().queryByRole("button", { name: "Points corrections" })).toBeNull();
    expect(await screen.findByText("Not set up yet")).toBeInTheDocument();
    expect(screen.getByText("The competition close time (17:30)")).toBeInTheDocument();
    expect(await screen.findByText("Late scans waiting for review: 2")).toBeInTheDocument();
    expect(screen.getByText("No final snapshot yet.")).toBeInTheDocument();
    expect(screen.getByText("Administrators only.")).toBeInTheDocument();
  });

  it("opens the existing Qmeter desk, and no ledger, for a Game Master", async () => {
    server(() => json(200, GM));
    render(<OrganiserPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Qmeter" }));
    expect(await screen.findByRole("region", { name: "Qmeter questions" })).toBeInTheDocument();
    expect(screen.getByText(/not connected to the Qmeter service/)).toBeInTheDocument();
  });

  it("lets a Game Master correct points from the organiser endpoints, without the CSV exports", async () => {
    const calls = server(() => json(200, { ...GM, sections: { ...GM.sections, corrections: true } }));
    render(<OrganiserPanel />);
    await screen.findByRole("navigation", { name: "Organiser sections" });
    fireEvent.click(nav().getByRole("button", { name: "Points corrections" }));
    expect(await screen.findByText(/The ledger itself is never edited/)).toBeInTheDocument();
    await vi.waitFor(() => expect(calls.some((u) => u.startsWith("/api/staff/points-log"))).toBe(true));
    await vi.waitFor(() => expect(calls.some((u) => u.startsWith("/api/staff/connections/unpaid"))).toBe(true));
    expect(calls.some((u) => u.startsWith("/api/admin/"))).toBe(false);
    expect(screen.queryByRole("button", { name: /Export CSV/ })).toBeNull();
  });

  it("gives an administrator the points ledger and the panel codes, and no Qmeter", async () => {
    const calls = server(() => json(200, ADMIN));
    render(<OrganiserPanel />);
    await screen.findByRole("navigation", { name: "Organiser sections" });
    expect(nav().queryByRole("button", { name: "Qmeter" })).toBeNull();
    fireEvent.click(nav().getByRole("button", { name: "Points corrections" }));
    expect(await screen.findByText(/The ledger itself is never edited/)).toBeInTheDocument();
    await vi.waitFor(() => expect(calls.some((u) => u.startsWith("/api/staff/points-log"))).toBe(true));

    fireEvent.click(nav().getByRole("button", { name: "Venue screens" }));
    const live = await screen.findAllByRole("link", { name: "Open screen" });
    expect(live.map((a) => a.getAttribute("href"))).toEqual(expect.arrayContaining(["/screen/live", "/screen/final", "/screen/photos"]));
    for (const a of live) expect(a).toHaveAttribute("target", "_blank");
    expect(await screen.findByText(/Panel 1/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open screen" }).map((a) => a.getAttribute("href"))).toContain("/screen/panel?k=3.1763064000.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  });

  it("lists no panel codes for a Game Master: those need an administrator", async () => {
    const calls = server(() => json(200, GM));
    render(<OrganiserPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Venue screens" }));
    await screen.findAllByRole("link", { name: "Open screen" });
    expect(screen.queryByText("Panel QR codes (Main Stage)")).toBeNull();
    expect(calls).not.toContain("/api/admin/panel-screen-links");
  });
});
