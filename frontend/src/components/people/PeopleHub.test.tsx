import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PeopleHub } from "./PeopleHub";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const PERSON = {
  user_id: 9, name: "Aysel M.", profession: "Data lead", score: 70,
  shared_topics: ["data"], complement: ["mentor", "mentoring"], shared_masterclasses: 0, same_sector: false,
  reasons: ["complement", "topics"],
};
const MEETING = {
  id: 3, status: "accepted", direction: "incoming", other: { user_id: 9, name: "Aysel Məmmədova", profession: "Data lead" },
  meeting: { table: "M2", starts_at: "2026-11-13T06:15:00Z", ends_at: "2026-11-13T06:30:00Z", prompts: ["topic:data", "goal:mentor", "intro"] },
};

function server(opts: { people?: unknown[]; requests?: unknown[]; connections?: unknown[]; write?: (url: string, body: unknown) => Response }) {
  const writes: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      writes.push({ url, body });
      return opts.write ? opts.write(url, body) : json(200, {});
    }
    if (url.endsWith("/people")) return json(200, { people: opts.people ?? [] });
    if (url.endsWith("/meetings")) return json(200, { timezone: "Asia/Baku", requests: opts.requests ?? [] });
    return json(200, { connections: opts.connections ?? [] });
  }));
  return writes;
}

afterEach(() => vi.unstubAllGlobals());

describe("PeopleHub", () => {
  it("shows a compatible person with the reasons and no contact data, and sends one request", async () => {
    const writes = server({ people: [PERSON] });
    render(<PeopleHub />);
    const card = within(await screen.findByTestId("person-9"));
    expect(card.getByText("70%")).toBeInTheDocument();
    expect(card.getByText("One of you offers what the other is looking for.")).toBeInTheDocument();
    expect(card.queryByText(/@|linkedin/i)).toBeNull();
    const button = card.getByRole("button", { name: "Let's meet" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]?.url).toBe("/api/attendees/me/meeting-requests");
    expect((writes[0]?.body as { recipient_id: number }).recipient_id).toBe(9);
  });

  it("explains why there is nobody yet instead of inventing people", async () => {
    server({});
    render(<PeopleHub />);
    expect(await screen.findByText(/No strong match yet/)).toBeInTheDocument();
  });

  it("shows the confirmed table, the event-time slot and the prompts", async () => {
    server({ requests: [MEETING] });
    render(<PeopleHub />);
    const meetings = within(await screen.findByRole("region", { name: "Match Corner meetings" }));
    expect(meetings.getByText("10:15–10:30")).toBeInTheDocument();
    expect(meetings.getByText("Table M2")).toBeInTheDocument();
    expect(meetings.getByText("Share one experience with Data.")).toBeInTheDocument();
    expect(meetings.getByText("Talk about A mentor: what would help each of you?")).toBeInTheDocument();
  });

  it("names the daily limit the server enforces", async () => {
    server({ people: [PERSON], write: () => json(409, { error: { code: "PEOPLE_CONFLICT", message: "", details: { reason: "daily_limit" } } }) });
    render(<PeopleHub />);
    fireEvent.click(within(await screen.findByTestId("person-9")).getByRole("button", { name: "Let's meet" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You can send up to 10 meeting requests a day.");
  });

  it("says when the scan connected the pair", async () => {
    server({ write: () => json(200, { connected: true, name: "Aysel M.", kind: "other", points: 1, outcome: "awarded" }) });
    render(<PeopleHub />);
    await screen.findByRole("region", { name: "Connect" });
    fireEvent.change(screen.getByLabelText("Their attendee code"), { target: { value: "A".repeat(43) } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Connected. +1 points for you. Their contact is below.");
  });

  it("explains a zero-point connection instead of showing the pair's value", async () => {
    server({ write: () => json(200, { connected: true, mutual: true, name: "Aysel M.", kind: "match", points: 0, outcome: "not_configured" }) });
    render(<PeopleHub />);
    await screen.findByRole("region", { name: "Connect" });
    fireEvent.change(screen.getByLabelText("Their attendee code"), { target: { value: "A".repeat(43) } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("No points yet: the competition is not set up yet.");
    expect(status.textContent).not.toMatch(/\+3|\+1/);
  });
});
