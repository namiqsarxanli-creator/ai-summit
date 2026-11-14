import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MasterclassAdmin } from "./MasterclassAdmin";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const PROGRAM = {
  events: [{ id: 1, code: "ai-summit-2026", title: "AI Summit 2026", venue: "The Ritz-Carlton Baku", timezone: "Asia/Baku", event_date: "2026-11-13" }],
  blocks: [{ id: 1, event_id: 1, code: "b1", title: "Blok 1" }],
  rooms: [{ id: 1, event_id: 1, name: "Zal 1", sort_order: 1 }],
  masterclasses: [
    { id: 5, event_id: 1, block_id: 1, room_id: 1, title: "Data for AI", capacity: 40, waitlist_capacity: 10, is_published: true, confirmed_count: 40, waitlist_count: 2 },
    { id: 6, event_id: 1, block_id: null, room_id: null, title: "LOGO masterclass", capacity: 40, waitlist_capacity: 10, is_published: false, confirmed_count: 0, waitlist_count: 0 },
  ],
};

type Call = { url: string; method: string; body: unknown };

function server(write: (call: Call) => Response) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/admin/masterclass-program") return json(200, PROGRAM);
    if (url === "/api/admin/masterclasses/5/reservations") {
      return json(200, {
        masterclass: PROGRAM.masterclasses[0],
        confirmed_count: 1,
        waitlist_count: 2,
        confirmed: [{ reservation_id: 1, user_id: 1, name: "Ada Lovelace", email: "ada@x.test" }],
        waitlist: [
          { reservation_id: 2, user_id: 2, name: "First Waiting", email: "w1@x.test", position: 1 },
          { reservation_id: 3, user_id: 3, name: "Second Waiting", email: "w2@x.test", position: 2 },
        ],
      });
    }
    const call = { url, method, body: init?.body ? JSON.parse(init.body as string) : null };
    calls.push(call);
    return write(call);
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

// The first form on the page is the masterclass form; labels repeat in the others.
const field = (label: string) => screen.getAllByLabelText(label)[0]!;

describe("MasterclassAdmin", () => {
  it("lists the programme with confirmed and waiting counts, and the queue in order", async () => {
    server(() => json(200, {}));
    render(<MasterclassAdmin />);
    expect(await screen.findByText("40/40")).toBeInTheDocument();
    expect(screen.getByText("2/10")).toBeInTheDocument();
    expect(screen.getByText("Not scheduled")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Show reservations" })[0]!);
    const items = await screen.findAllByText(/Waiting · w\d@x\.test/);
    expect(items.map((i) => i.textContent)).toEqual(["First Waiting · w1@x.test", "Second Waiting · w2@x.test"]);
  });

  it("sends only the fields filled in when editing, and names the refusal reason", async () => {
    const calls = server(() =>
      json(409, { error: { code: "MASTERCLASS_ADMIN_CONFLICT", message: "", details: { reason: "capacity_below_confirmed", confirmed: 40 }, correlation_id: "c" } }),
    );
    render(<MasterclassAdmin />);
    await screen.findByText("40/40");
    fireEvent.change(field("Masterclass ID (empty to add a new one)"), { target: { value: "5" } });
    fireEvent.change(field("Capacity"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save masterclass" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The capacity cannot be below the seats already confirmed.");
    expect(calls).toEqual([{ url: "/api/admin/masterclasses/5", method: "PATCH", body: { capacity: 30 } }]);
  });

  it("creates a masterclass without a slot, hidden until published", async () => {
    const calls = server(() => json(201, { id: 7 }));
    render(<MasterclassAdmin />);
    await screen.findByText("40/40");
    fireEvent.change(field("Event ID"), { target: { value: "1" } });
    fireEvent.change(field("Title"), { target: { value: "LOGO masterclass 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save masterclass" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({
      url: "/api/admin/masterclasses",
      method: "POST",
      body: { event_id: 1, block_id: null, room_id: null, title: "LOGO masterclass 2", is_published: false },
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Change saved and recorded in the admin audit.");
  });
});


it("changes standard labels only when explicitly requested and can restore the custom name", async () => {
  const calls = server(() => json(200, {}));
  render(<MasterclassAdmin />);
  await screen.findByText("40/40");
  fireEvent.change(screen.getByLabelText("Room ID (empty to add a new one)"), {target:{value:"1"}});
  fireEvent.change(screen.getByLabelText("Room name"), {target:{value:"Custom Hall"}});
  fireEvent.click(screen.getByRole("button", {name:"Save room"}));
  await screen.findByRole("status");
  expect(calls[0]).toMatchObject({method:"PATCH",body:{name:"Custom Hall"}});
  expect(calls[0]?.body).not.toHaveProperty("display_number");
  fireEvent.change(screen.getByLabelText("Standard room number (optional)"), {target:{value:"3"}});
  fireEvent.change(screen.getByLabelText("Room label"), {target:{value:"standard"}});
  fireEvent.click(screen.getByRole("button", {name:"Save room"}));
  await waitFor(()=>expect(calls).toHaveLength(2));
  expect(calls[1]?.body).toMatchObject({display_number:3});
  await waitFor(()=>expect(screen.getByRole("button",{name:"Save room"})).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Room label"), {target:{value:"custom"}});
  fireEvent.click(screen.getByRole("button", {name:"Save room"}));
  await waitFor(()=>expect(calls).toHaveLength(3));
  expect(calls[2]?.body).toMatchObject({display_number:null});
});

it("does not send an invalid standard number", async () => {
  const calls = server(() => json(200, {}));
  render(<MasterclassAdmin />);
  await screen.findByText("40/40");
  fireEvent.change(screen.getByLabelText("Standard block number (optional)"), {target:{value:"1.5"}});
  fireEvent.click(screen.getByRole("button", {name:"Save block"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("between 1 and 10000");
  expect(calls).toHaveLength(0);
});
