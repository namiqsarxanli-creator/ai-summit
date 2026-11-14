import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PlaygroundSlot } from "@/lib/playground";

import { LanguageProvider } from "@/components/ui/LanguageProvider";
import { PlaygroundProgram } from "./PlaygroundProgram";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const conflict = (reason: string) => json(409, { error: { code: "PLAYGROUND_CONFLICT", message: "", details: { reason } } });

const slot = (over: Partial<PlaygroundSlot> = {}): PlaygroundSlot => ({
  id: 1,
  event_id: 1,
  booth_id: 1,
  booth: "AIWorks",
  number: 1,
  starts_at: "2026-11-13T05:00:00Z",
  ends_at: "2026-11-13T05:15:00Z",
  timezone: "Asia/Baku",
  seats_left: 3,
  bookable: true,
  queue_open: true,
  cancellable: false,
  mine: null,
  revision: "0",
  invitations: [],
  queue_position: null,
  ...over,
});

type Write = { path: string; body: unknown };

function server(opts: { slots?: () => PlaygroundSlot[]; programme?: () => Response; write?: (w: Write) => Response | Promise<Response> } = {}) {
  const writes: Write[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const w = { path, body: JSON.parse(String(init.body)) };
      writes.push(w);
      return opts.write ? opts.write(w) : json(200, {});
    }
    if (opts.programme) return opts.programme();
    return json(200, { server_time: "x", slots: (opts.slots ?? (() => [slot()]))() });
  }));
  return writes;
}

const card = async (id = 1, place: "slot" | "mine" = "slot") => within(await screen.findByTestId(`pg-${place}-${id}`));

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("PlaygroundProgram", () => {
  it("shows a sign-in action rather than an endless loader", async () => {
    server({ programme: () => json(401, { error: { code: "UNAUTHENTICATED" } }) });
    render(<PlaygroundProgram />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading Playground…");
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the server error with a retry", async () => {
    let fail = true;
    server({ programme: () => (fail ? json(503, { error: { code: "MAINTENANCE" } }) : json(200, { server_time: "x", slots: [slot()] })) });
    render(<PlaygroundProgram />);
    expect(await screen.findByRole("alert")).toHaveTextContent("briefly unavailable");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("pg-slot-1")).toBeInTheDocument();
  });

  it("shows the chosen booth's one-sentence card, in the chosen language", async () => {
    window.localStorage.setItem("facemark.language", "az");
    try {
      server({ slots: () => [slot({ booth: "HANDEX — Can You Guess?", booth_description: "You draw, AI guesses! Over three rounds, AI analyses your sketches and shares its guesses. Succeed in at least two rounds to reach the booth final and race for the fastest time." })] });
      render(<LanguageProvider><PlaygroundProgram /></LanguageProvider>);
      expect(await screen.findByTestId("booth-about")).toHaveTextContent("Sən çək, AI tapsın!");
      expect(screen.getAllByText("HANDEX — Görək, bildin?").length).toBeGreaterThan(0);
    } finally {
      window.localStorage.removeItem("facemark.language");
    }
  });

  it("says when no sessions are open yet", async () => {
    server({ slots: () => [] });
    render(<PlaygroundProgram />);
    expect(await screen.findByText("The organiser has not opened Playground bookings yet.")).toBeInTheDocument();
  });

  it("books once on a double tap, with the observed revision, and shows success in the card", async () => {
    let saved = false;
    let release: (r: Response) => void = () => {};
    const writes = server({
      slots: () => [saved ? slot({ cancellable: true, revision: "11:1", seats_left: 2, mine: { id: 11, slot_id: 1, status: "confirmed", version: 1, expires_at: null } }) : slot()],
      write: () => new Promise<Response>((resolve) => { release = resolve; }),
    });
    render(<PlaygroundProgram />);
    const button = (await card()).getByRole("button", { name: "Reserve 15 minutes" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(writes).toHaveLength(1);
    expect(writes[0]?.body).toMatchObject({ expected_revision: "0", queue: false });
    saved = true;
    await act(async () => release(json(200, { id: 11 })));
    const mine = within(await screen.findByRole("region", { name: "My Playground bookings" }));
    expect(await mine.findByText("Place reserved")).toBeInTheDocument();
    expect(screen.getAllByText("Your place is reserved.").length).toBeGreaterThan(0);
    expect(writes).toHaveLength(1);
  });

  it("refreshes after a conflict and never retries the write by itself", async () => {
    const writes = server({ write: () => conflict("choice_changed") });
    render(<PlaygroundProgram />);
    fireEvent.click((await card()).getByRole("button", { name: "Reserve 15 minutes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your booking changed");
    expect(writes).toHaveLength(1);
  });

  it("offers only the on-site queue when no place can be booked, and walk-in sessions say so", async () => {
    server({ slots: () => [slot({ seats_left: 0 }), slot({ id: 2, number: 35, bookable: false, seats_left: 3 })] });
    render(<PlaygroundProgram />);
    const full = await card(1);
    expect(full.getByText("0 of 3 places free")).toBeInTheDocument();
    expect(full.queryByRole("button", { name: "Reserve 15 minutes" })).toBeNull();
    expect(full.getByRole("button", { name: "Join the on-site queue" })).toBeInTheDocument();
    const walkIn = await card(2);
    expect(walkIn.getByText("Walk-in only · no advance booking")).toBeInTheDocument();
    expect(walkIn.queryByRole("button", { name: "Reserve 15 minutes" })).toBeNull();
  });

  it("explains a released place after a late arrival and lets the attendee queue again", async () => {
    const writes = server({ slots: () => [slot({ bookable: false, seats_left: 1, revision: "9:2", mine: { id: 9, slot_id: 1, status: "no_show", version: 2, expires_at: null } })] });
    render(<PlaygroundProgram />);
    const c = await card(1, "mine");
    expect(c.getAllByText("Reserved place expired").length).toBeGreaterThan(0);
    expect(c.getByText(/more than 3 minutes late/)).toBeInTheDocument();
    fireEvent.click(c.getByRole("button", { name: "Join the on-site queue" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]?.body).toMatchObject({ expected_revision: "9:2", queue: true });
  });

  it("shows the queue place and lets the attendee leave the queue", async () => {
    const writes = server({ slots: () => [slot({ seats_left: 0, cancellable: true, queue_position: 2, revision: "5:1", mine: { id: 5, slot_id: 1, status: "waitlisted", version: 1, expires_at: null } })] });
    render(<PlaygroundProgram />);
    const mine = within(await screen.findByRole("region", { name: "My Playground bookings" }));
    expect(mine.getByText("On-site queue · place 2")).toBeInTheDocument();
    fireEvent.click(mine.getByRole("button", { name: "Leave the queue" }));
    await waitFor(() => expect(writes[0]).toEqual({ path: "/api/playground/bookings/5/cancel", body: { expected_version: 1 } }));
  });

  it("creates an invitation link in the URL fragment, never in a query", async () => {
    server({
      slots: () => [slot({ seats_left: 2, mine: { id: 11, slot_id: 1, status: "confirmed", version: 1, expires_at: null }, revision: "11:1", cancellable: true })],
      write: () => json(200, { id: 12, slot_id: 1, status: "invited", version: 1, expires_at: "2026-11-13T04:30:00Z", token: "12.abc" }),
    });
    render(<PlaygroundProgram />);
    fireEvent.click((await card(1, "mine")).getByRole("button", { name: "Invite a teammate" }));
    const field = (await screen.findByLabelText("Invitation link")) as HTMLInputElement;
    expect(field.value).toMatch(/\/playground#invite=12\.abc$/);
    expect(field.value).not.toContain("?");
  });

  it("does not accept an invitation before the attendee confirms it, and 'Not now' keeps it unaccepted", async () => {
    window.history.replaceState(null, "", "/playground#invite=long-enough-test-invitation");
    let accepted = 0;
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      if (path.endsWith("/inspect")) return json(200, { slot_id: 1, booth_id: 1, starts_at: "2026-11-13T05:00:00Z", ends_at: "2026-11-13T05:15:00Z", expires_at: "2026-11-13T04:30:00Z" });
      if (path.endsWith("/accept")) { accepted++; return json(200, { id: 11 }); }
      return json(200, { server_time: "x", slots: [slot()] });
    }));
    render(<PlaygroundProgram />);
    const invitation = within(await screen.findByRole("region", { name: "Team invitation" }));
    expect(invitation.getByText("AIWorks · P01 · 09:00–09:15")).toBeInTheDocument();
    expect(invitation.getByText("Accept before 08:30 to keep the place.")).toBeInTheDocument();
    expect(accepted).toBe(0);
    fireEvent.click(invitation.getByRole("button", { name: "Accept invitation" }));
    await waitFor(() => expect(accepted).toBe(1));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Team invitation" })).toBeNull());
    expect(window.location.hash).toBe("");
  });

  it("notices an invitation link opened while the page is already showing", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) =>
      path.endsWith("/inspect")
        ? json(200, { slot_id: 1, booth_id: 1, starts_at: "2026-11-13T05:00:00Z", ends_at: "2026-11-13T05:15:00Z", expires_at: "2026-11-13T04:30:00Z" })
        : json(200, { server_time: "x", slots: [slot()] })));
    render(<PlaygroundProgram />);
    await screen.findByTestId("pg-slot-1");
    expect(screen.queryByRole("region", { name: "Team invitation" })).toBeNull();
    act(() => {
      window.history.replaceState(null, "", "/playground#invite=long-enough-test-invitation");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(await screen.findByRole("region", { name: "Team invitation" })).toBeInTheDocument();
  });

  it("names an invitation that is no longer valid", async () => {
    window.history.replaceState(null, "", "/playground#invite=long-enough-test-invitation");
    vi.stubGlobal("fetch", vi.fn(async (path: string) =>
      path.endsWith("/inspect") ? conflict("invitation_expired") : json(200, { server_time: "x", slots: [slot()] })));
    render(<PlaygroundProgram />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This invitation has expired or was withdrawn.");
    expect(screen.queryByRole("button", { name: "Accept invitation" })).toBeNull();
  });
});
