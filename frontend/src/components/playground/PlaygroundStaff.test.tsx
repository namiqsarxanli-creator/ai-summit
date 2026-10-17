import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlaygroundStaff } from "./PlaygroundStaff";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const PROGRAMME = {
  server_time: "x",
  slots: [{ id: 7, event_id: 1, booth_id: 1, booth: "AIWorks", number: 3, starts_at: "2026-11-13T05:30:00Z", ends_at: "2026-11-13T05:45:00Z", timezone: "Asia/Baku", seats_left: 1, bookable: false, queue_open: true, cancellable: false, mine: null, revision: "0", invitations: [], queue_position: null }],
};

function server(admit: () => Response) {
  const admits: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/api/staff/playground") return json(200, PROGRAMME);
    if (path === "/api/staff/playground/slots/7") {
      return json(200, { slot_id: 7, bookings: [{ id: 21, slot_id: 7, status: "confirmed", version: 1, expires_at: null, name: "Leyla Məmmədova" }] });
    }
    admits.push(JSON.parse(String(init?.body)));
    return admit();
  }));
  return admits;
}

afterEach(() => vi.unstubAllGlobals());

describe("PlaygroundStaff", () => {
  it("lists the session and confirms an arrival once, with the observed version", async () => {
    const admits = server(() => json(200, { id: 21, slot_id: 7, status: "checked_in", version: 2, expires_at: null, admitted: true }));
    render(<PlaygroundStaff />);
    expect(await screen.findByRole("option", { name: "AIWorks · P03 · 09:30" })).toBeInTheDocument();
    const button = await screen.findByRole("button", { name: "Confirm arrival" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(await screen.findByRole("status")).toHaveTextContent("Arrival confirmed.");
    expect(admits).toEqual([{ expected_version: 1 }]);
  });

  it("explains a late reserved attendee instead of pretending they were admitted", async () => {
    server(() => json(200, { id: 21, slot_id: 7, status: "no_show", version: 2, expires_at: null, admitted: false, reason: "late_arrival" }));
    render(<PlaygroundStaff />);
    fireEvent.click(await screen.findByRole("button", { name: "Confirm arrival" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("More than 3 minutes late");
  });

  it("shows the server's refusal for another booth", async () => {
    server(() => json(403, { error: { code: "STAFF_NOT_AUTHORIZED", message: "", details: {} } }));
    render(<PlaygroundStaff />);
    fireEvent.click(await screen.findByRole("button", { name: "Confirm arrival" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("This account cannot access that area."));
  });
});
