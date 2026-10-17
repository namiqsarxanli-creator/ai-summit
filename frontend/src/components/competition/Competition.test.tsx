import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AttendanceDesk } from "./AttendanceDesk";
import { PanelScan } from "./PanelScan";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const refused = (code: string, reason?: string) =>
  json(409, { error: { code, message: "", details: reason ? { reason } : {} } });

const CODE = `P1.ENTRY.1.${"a".repeat(64)}`;
const MINE = {
  eligible: true,
  total: 13,
  maximum: 100,
  categories: [
    { category: "PROFILE", points: 5, maximum: 5 },
    { category: "PANEL", points: 8, maximum: 16 },
  ],
};
const PANELS = [{ id: 1, title: "Panel 1", starts_at: "2026-11-13T06:00:00Z", ends_at: "2026-11-13T07:30:00Z", timezone: "Asia/Baku" }];

afterEach(() => vi.unstubAllGlobals());

function typeAndSend(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("PanelScan", () => {
  it("shows own points per category against each maximum", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url === "/api/panels" ? json(200, PANELS) : json(200, MINE))));
    render(<PanelScan />);
    const points = within(await screen.findByRole("region", { name: "My points" }));
    expect(points.getByText("13 of 100")).toBeInTheDocument();
    expect(points.getByText("8 of 16")).toBeInTheDocument();
    expect(screen.getByText(/10:00–11:30/)).toBeInTheDocument(); // event time zone, from the server
  });

  it("asks a visitor to sign in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(401, { error: { code: "UNAUTHENTICATED" } })));
    render(<PanelScan />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("refuses a non-panel code without calling the server, then records an entry once", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return json(200, { panel_id: 1, phase: "entry", recorded: ["entry"], award: null });
      }
      return url === "/api/panels" ? json(200, PANELS) : json(200, MINE);
    }));
    render(<PanelScan />);
    await screen.findByRole("region", { name: "My points" });
    typeAndSend("Code from the screen", "https://example.com/not-a-panel");
    expect(await screen.findByRole("alert")).toHaveTextContent("This is not a panel code");
    expect(posts).toHaveLength(0);
    typeAndSend("Code from the screen", CODE);
    expect(await screen.findByRole("status")).toHaveTextContent("Entry recorded. Scan the exit code");
    expect(posts).toEqual([{ code: CODE }]);
  });

  it("names the window rule when a scan is late", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return refused("PANEL_SCAN_REFUSED", "outside_window");
      return url === "/api/panels" ? json(200, PANELS) : json(200, MINE);
    }));
    render(<PanelScan />);
    await screen.findByRole("region", { name: "My points" });
    typeAndSend("Code from the screen", CODE);
    expect(await screen.findByRole("alert")).toHaveTextContent("This scan is outside its time window.");
  });
});

const TOKEN = "A".repeat(43);

function desk(opts: { role?: string; scopes?: unknown[]; write?: (url: string, body: unknown) => Response }) {
  const writes: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      writes.push({ url, body });
      return opts.write ? opts.write(url, body) : json(200, {});
    }
    if (url === "/api/staff/me") return json(200, { full_name: "Vol", role: opts.role ?? "VOLUNTEER", booth_name: null });
    if (url === "/api/staff/me/scopes") return json(200, { scopes: opts.scopes ?? [] });
    if (url === "/api/panels") return json(200, PANELS);
    if (url === "/api/masterclasses") return json(200, { server_time: "x", events: [] });
    return json(200, { server_time: "x", slots: [] });
  }));
  return writes;
}

describe("AttendanceDesk", () => {
  it("says so when no job is assigned", async () => {
    desk({});
    render(<AttendanceDesk />);
    expect(await screen.findByText("No scanning job is assigned to this account. Ask an administrator.")).toBeInTheDocument();
  });

  it("scans a masterclass door with the chosen phase and shows the name", async () => {
    const writes = desk({
      scopes: [{ scope: "MASTERCLASS_DOOR", target_id: 7 }],
      write: () => json(200, { phase: "exit", name: "Leyla M.", standing: false, award: { outcome: "awarded", points: 12 } }),
    });
    render(<AttendanceDesk />);
    await screen.findByRole("region", { name: "Masterclass door" });
    fireEvent.click(screen.getByLabelText("Exit (last 10 minutes)"));
    typeAndSend("Attendee code", `https://summit.example/c/${TOKEN}`);
    expect(await screen.findByRole("status")).toHaveTextContent("Leyla M. · Points added.");
    expect(writes).toEqual([{ url: "/api/staff/masterclasses/7/door", body: { card_token: TOKEN, phase: "EXIT" } }]);
  });

  it("identifies by name and phone before recording a Qmeter question", async () => {
    const writes = desk({
      scopes: [{ scope: "GAME_MASTER", target_id: null }],
      write: (url) => url.endsWith("/identify")
        ? json(200, { user_id: 4, name: "Aysel Məmmədova", phone: "•••2233" })
        : json(200, { outcome: "awarded", points: 2 }),
    });
    render(<AttendanceDesk />);
    await screen.findByRole("region", { name: "Qmeter questions" });
    expect(screen.queryByRole("button", { name: "Record the question" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Full name"), { target: { value: "Aysel Məmmədova" } });
    fireEvent.change(screen.getByLabelText("Phone"), { target: { value: "+994 50 111 22 33" } });
    fireEvent.click(screen.getByRole("button", { name: "Find the attendee" }));
    expect(await screen.findByText("Aysel Məmmədova · •••2233")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("What you checked in Qmeter"), { target: { value: "Question 14" } });
    fireEvent.click(screen.getByRole("button", { name: "Record the question" }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1]).toEqual({ url: "/api/staff/qmeter/award", body: { user_id: 4, panel_id: 1, reason: "Question 14" } });
  });
});
