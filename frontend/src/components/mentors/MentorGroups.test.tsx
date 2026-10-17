import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MentorGroups } from "./MentorGroups";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const SESSION = {
  id: 4, title: "Careers in AI", mentor_name: "Configured Mentor",
  starts_at: "2026-11-13T09:10:00Z", ends_at: "2026-11-13T09:30:00Z",
  capacity: 6, seats_left: 2, waitlist_left: 3, bookable: true, cancellable: true, revision: "0", booking: null,
};

function server(sessions: unknown[], write?: (url: string, body: unknown) => Response) {
  const writes: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      writes.push({ url, body });
      return write ? write(url, body) : json(200, { id: 1, status: "confirmed", version: 1 });
    }
    return json(200, { timezone: "Asia/Baku", sessions });
  }));
  return writes;
}

afterEach(() => vi.unstubAllGlobals());

describe("MentorGroups", () => {
  it("shows the event-time slot and places left, and books once with the observed revision", async () => {
    const writes = server([SESSION]);
    render(<MentorGroups />);
    const card = within(await screen.findByTestId("mentor-4"));
    expect(card.getByText("13:10–13:30")).toBeInTheDocument();
    expect(card.getByText("Configured Mentor")).toBeInTheDocument();
    expect(card.getByText("2 of 6 places left")).toBeInTheDocument();
    const button = card.getByRole("button", { name: "Join this group" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]?.url).toBe("/api/mentor-sessions/4/book");
    expect((writes[0]?.body as { expected_revision: string }).expected_revision).toBe("0");
    expect(await card.findByRole("status")).toHaveTextContent("Your place is confirmed.");
  });

  it("offers the waiting list when the group is full and shows the place in line", async () => {
    server([
      { ...SESSION, seats_left: 0 },
      { ...SESSION, id: 5, seats_left: 0, booking: { id: 9, session_id: 5, status: "waitlisted", version: 1, position: 2 } },
    ]);
    render(<MentorGroups />);
    const full = within(await screen.findByTestId("mentor-4"));
    expect(full.getByText("Group full · waiting list open")).toBeInTheDocument();
    // One active place at a time: the other group cannot be joined meanwhile.
    expect(full.getByRole("button", { name: "Join the waiting list" })).toBeDisabled();
    const mine = within(screen.getByTestId("mentor-5"));
    expect(mine.getByText(/You are on the waiting list\. Place in line: 2/)).toBeInTheDocument();
    expect(mine.getByRole("button", { name: "Cancel my place" })).toBeEnabled();
  });

  it("names the server's reason and never claims points", async () => {
    server([SESSION], () => json(409, { error: { code: "MENTOR_CONFLICT", message: "", details: { reason: "playground_overlap" } } }));
    render(<MentorGroups />);
    fireEvent.click(within(await screen.findByTestId("mentor-4")).getByRole("button", { name: "Join this group" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This time overlaps your AI Playground booking.");
    expect(screen.queryByText(/\+5 points added/i)).toBeNull();
  });

  it("offers nothing once a session has started, and shows the host's confirmation", async () => {
    server([{ ...SESSION, bookable: false, cancellable: false, booking: { id: 9, session_id: 4, status: "attended", version: 2, position: null } }]);
    render(<MentorGroups />);
    const card = within(await screen.findByTestId("mentor-4"));
    expect(card.getByText("The host confirmed your participation.")).toBeInTheDocument();
    expect(card.queryByRole("button")).toBeNull();
  });

  it("says plainly when no group is published", async () => {
    server([]);
    render(<MentorGroups />);
    expect(await screen.findByText("The organiser has not published mentor groups yet.")).toBeInTheDocument();
  });
});
