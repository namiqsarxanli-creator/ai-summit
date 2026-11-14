import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationBell, NotificationList } from "./Notifications";
import { describe as word, unreadCount, type Notification } from "@/lib/notifications";

const FEED: { server_time: string; timezone: string; items: Notification[] } = {
  server_time: "2026-11-13T07:00:00+00:00",
  timezone: "Asia/Baku",
  items: [
    { id: "programme_soon:masterclass:5", kind: "programme_soon", at: "2026-11-13T06:30:00+00:00", href: "/programme", params: { item: "masterclass", title: "Data for AI", place: "Ağdam Room", starts_at: "2026-11-13T07:30:00+00:00" } },
    { id: "scan_back:4", kind: "scan_back", at: "2026-11-13T06:10:00+00:00", href: "/my-card", params: { name: "Leyla M." } },
    { id: "points:9", kind: "points", at: "2026-11-12T10:00:00+00:00", href: "/points", params: { source: "PANEL", points: 8, reversal: false } },
  ],
};

function serve(body: unknown, status = 200) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })));
}

describe("notifications", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("words each kind, in the event's time zone", () => {
    const t = (k: string) => k;
    const [soon, scan, points] = FEED.items as [Notification, Notification, Notification];
    expect(word(soon, t, "Asia/Baku", "en")).toEqual({ title: "Starts at 11:30", detail: "Masterclass · Data for AI · Ağdam Room" });
    expect(word(scan, t, "Asia/Baku", "en").title).toBe("Leyla M. scanned your QR");
    expect(word(points, t, "Asia/Baku", "en")).toEqual({ title: "+8 points", detail: "Main Stage panels" });
    expect(word({ ...points, params: { source: "PANEL", points: -8, reversal: true } }, t, "Asia/Baku", "en").title).toBe("8 points taken back");
  });

  it("counts only what arrived after the list was last opened", () => {
    const items = FEED.items;
    expect(unreadCount(items, 0)).toBe(3);
    expect(unreadCount(items, Date.parse("2026-11-13T06:20:00Z"))).toBe(1);
  });

  it("lists the items newest first, marks the new ones and then remembers them as seen", async () => {
    window.localStorage.setItem("fm-notifications-seen", String(Date.parse("2026-11-13T06:20:00Z")));
    serve(FEED);
    render(<NotificationList />);
    const links = await screen.findAllByRole("link", { name: /Starts at|scanned your QR|points/ });
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/programme", "/my-card", "/points"]);
    expect(screen.getAllByText("New")).toHaveLength(1);
    expect(window.localStorage.getItem("fm-notifications-seen")).toBe(String(Date.parse(FEED.server_time)));
  });

  it("shows the bell with the number of new items, and nothing to a visitor who is signed out", async () => {
    serve(FEED);
    const { unmount } = render(<NotificationBell />);
    expect(await screen.findByRole("link", { name: "Notifications: 3 new" })).toHaveAttribute("href", "/notifications");
    unmount();
    serve({ error: { code: "UNAUTHENTICATED", message: "", correlation_id: "c" } }, 401);
    render(<NotificationBell />);
    await Promise.resolve();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
