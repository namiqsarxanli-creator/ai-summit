import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Choice, MasterclassItem, Program } from "@/lib/masterclasses";

import { MasterclassProgram } from "./MasterclassProgram";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const failure = (status: number, code: string, details: Record<string, unknown> = {}) =>
  json(status, { error: { code, message: "", details, correlation_id: "c" } });

function mc(id: number, over: Partial<MasterclassItem> = {}): MasterclassItem {
  return {
    id,
    title: `Masterclass ${id}`,
    description: "Hands-on session.",
    speaker: "Speaker To Be Confirmed",
    room: `Zal ${id}`,
    capacity: 40,
    waitlist_capacity: 10,
    confirmed_count: 10,
    waitlist_count: 0,
    seats_left: 30,
    availability: "available",
    ...over,
  };
}

function program(items: MasterclassItem[], open = true): Program {
  return {
    server_time: "2026-11-13T06:00:00Z",
    events: [
      {
        code: "ai-summit-2026",
        title: "AI Summit 2026 · Beyond Now",
        venue: "The Ritz-Carlton Baku",
        timezone: "Asia/Baku",
        event_date: "2026-11-13",
        blocks: [
          {
            id: 1,
            code: "b1",
            title: "Blok 1",
            starts_at: "2026-11-13T07:30:00Z",
            ends_at: "2026-11-13T09:00:00Z",
            change_deadline: "2026-11-13T07:00:00Z",
            changes_open: open,
            masterclasses: items,
          },
        ],
        unscheduled: [{ id: 99, title: "LOGO masterclass", description: null, speaker: null }],
      },
    ],
  };
}

const choice = (masterclass_id: number, status: Choice["status"], position: number | null = null): Choice => ({
  block_id: 1,
  masterclass_id,
  status,
  waitlist_position: position,
  change_deadline: "2026-11-13T07:00:00Z",
  changes_open: true,
});

type Call = { url: string; method: string; body: unknown };

function server(opts: {
  program?: () => Response;
  mine?: () => Response;
  write?: (call: Call) => Response | Promise<Response>;
}) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "GET" && url === "/api/masterclasses") return (opts.program ?? (() => json(200, program([mc(1)]))))();
    if (method === "GET" && url === "/api/attendees/me/masterclasses") {
      return (opts.mine ?? (() => json(200, { server_time: "x", choices: [] })))();
    }
    const call = { url, method, body: init?.body ? JSON.parse(init.body as string) : null };
    calls.push(call);
    return opts.write ? opts.write(call) : failure(500, "INTERNAL_ERROR");
  });
  vi.stubGlobal("fetch", fetchMock);
  return { writes: () => calls };
}

const result = (outcome: string, choices: Choice[]) => json(200, { outcome, choices: { server_time: "x", choices } });
const card = (id: number) => within(screen.getByTestId(`masterclass-${id}`));
const findCard = async (id: number) => within(await screen.findByTestId(`masterclass-${id}`));

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => vi.unstubAllGlobals());

describe("MasterclassProgram", () => {
  it("shows a loading state, then the programme in the event's time zone", async () => {
    server({});
    render(<MasterclassProgram />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading the masterclass programme…");
    expect(await screen.findByRole("heading", { name: "Masterclass 1" })).toBeInTheDocument();
    expect(screen.getAllByText("11:30–13:00").length).toBeGreaterThan(0);
    expect(screen.getByText("Choices can be changed until 11:00.")).toBeInTheDocument();
    expect(card(1).getByText("30 of 40 seats left")).toBeInTheDocument();
    expect(screen.getByText("LOGO masterclass")).toBeInTheDocument(); // to be scheduled, not bookable
    expect(screen.getByText("Booking opens once its time and room are set.")).toBeInTheDocument();
    expect(screen.queryByText(/attended|completed/i)).toBeNull();
  });

  it("shows the server's error with a retry", async () => {
    let fail = true;
    server({ program: () => (fail ? failure(503, "MAINTENANCE") : json(200, program([mc(1)]))) });
    render(<MasterclassProgram />);
    expect(await screen.findByRole("alert")).toHaveTextContent("briefly unavailable");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Masterclass 1" })).toBeInTheDocument();
  });

  it("lets a visitor read the programme and asks them to sign in to choose", async () => {
    server({ mine: () => failure(401, "UNAUTHENTICATED") });
    render(<MasterclassProgram />);
    await screen.findByRole("heading", { name: "Masterclass 1" });
    expect(screen.getByText("Sign in to choose your masterclasses.")).toBeInTheDocument();
    expect(card(1).queryByRole("button")).toBeNull();
  });

  it("books a free seat and shows it as confirmed (success)", async () => {
    const api = server({ write: () => result("confirmed", [choice(1, "confirmed")]) });
    render(<MasterclassProgram />);
    fireEvent.click(await (await findCard(1)).findByRole("button", { name: "Choose" }));
    expect(await screen.findByText("Your seat is confirmed.")).toBeInTheDocument();
    expect(api.writes()).toEqual([
      { url: "/api/attendees/me/masterclasses/1", method: "PUT", body: { masterclass_id: 1, expected_revision: "0", join_waitlist: false, give_up_confirmed_seat: false } },
    ]);
    const mine = within(screen.getByRole("region", { name: "My choices" }));
    expect(mine.getByText("Seat confirmed")).toBeInTheDocument();
    expect(card(1).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("joins a waitlist only when asked, and shows the place in the queue", async () => {
    const full = mc(2, { availability: "waitlist", seats_left: 0, confirmed_count: 40, waitlist_count: 3 });
    const api = server({
      program: () => json(200, program([full])),
      write: () => result("waitlisted", [choice(2, "waitlisted", 4)]),
    });
    render(<MasterclassProgram />);
    expect(await (await findCard(2)).findByText("Full · waitlist 3 of 10")).toBeInTheDocument();
    fireEvent.click(card(2).getByRole("button", { name: "Join the waitlist" }));
    expect(await screen.findByText("You are on the waitlist · place 4")).toBeInTheDocument();
    expect((api.writes()[0]?.body as { join_waitlist: boolean }).join_waitlist).toBe(true);
    expect(card(2).getByRole("button", { name: "Leave the waitlist" })).toBeInTheDocument();
  });

  it("shows 'No places left' when seats and waitlist are full", async () => {
    server({ program: () => json(200, program([mc(3, { availability: "full", seats_left: 0, waitlist_count: 10 })])) });
    render(<MasterclassProgram />);
    expect(await (await findCard(3)).findAllByText("No places left")).toHaveLength(1);
    expect(card(3).queryByRole("button")).toBeNull();
  });

  it("tells the attendee when the last seat went just before them", async () => {
    server({ write: () => failure(409, "MASTERCLASS_FULL", { waitlist_open: true }) });
    render(<MasterclassProgram />);
    fireEvent.click(await (await findCard(1)).findByRole("button", { name: "Choose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The last seat was just taken. You can join the waitlist instead.");
  });

  it("asks before giving up a confirmed seat for a waitlist place, and can keep the seat", async () => {
    const wanted = mc(2, { availability: "waitlist", seats_left: 0 });
    const api = server({
      program: () => json(200, program([mc(1), wanted])),
      mine: () => json(200, { server_time: "x", choices: [choice(1, "confirmed")] }),
      write: () => result("waitlisted", [choice(2, "waitlisted", 1)]),
    });
    render(<MasterclassProgram />);
    fireEvent.click(await (await findCard(2)).findByRole("button", { name: "Change and join the waitlist" }));
    const dialog = within(screen.getByRole("alertdialog"));
    expect(dialog.getByText(/You have a confirmed seat in “Masterclass 1”/)).toBeInTheDocument();
    expect(api.writes()).toHaveLength(0); // nothing sent before the answer
    fireEvent.click(dialog.getByRole("button", { name: "Keep my seat" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.writes()).toHaveLength(0);

    fireEvent.click(card(2).getByRole("button", { name: "Change and join the waitlist" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Give up my seat and join the waitlist" }));
    await waitFor(() => expect(api.writes()).toHaveLength(1));
    expect(api.writes()[0]?.body).toEqual({ masterclass_id: 2, expected_revision: "0", join_waitlist: true, give_up_confirmed_seat: true });
  });

  it("sends one request for a double tap", async () => {
    let release: (r: Response) => void = () => {};
    const api = server({ write: () => new Promise<Response>((resolve) => { release = resolve; }) });
    render(<MasterclassProgram />);
    const button = await (await findCard(1)).findByRole("button", { name: "Choose" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.writes()).toHaveLength(1);
    expect(card(1).getByRole("button", { name: "Saving…" })).toBeDisabled();
    await act(async () => release(result("confirmed", [choice(1, "confirmed")])));
    expect(await screen.findByText("Your seat is confirmed.")).toBeInTheDocument();
    expect(api.writes()).toHaveLength(1);
  });

  it("offers no change once the server says the block is closed", async () => {
    server({
      program: () => json(200, program([mc(1)], false)),
      mine: () => json(200, { server_time: "x", choices: [{ ...choice(1, "confirmed"), changes_open: false }] }),
    });
    render(<MasterclassProgram />);
    await screen.findByRole("heading", { name: "Masterclass 1" });
    expect(screen.getAllByText("Changes closed at 11:00.").length).toBeGreaterThan(0);
    expect(card(1).queryByRole("button")).toBeNull();
    expect(card(1).getByText("Changes are closed for this block.")).toBeInTheDocument();
  });

  it("names a server refusal in plain words", async () => {
    server({ write: () => failure(409, "MASTERCLASS_CHANGES_CLOSED") });
    render(<MasterclassProgram />);
    fireEvent.click(await (await findCard(1)).findByRole("button", { name: "Choose" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choices for this block can no longer be changed.");
  });
});


it("refreshes a promoted waitlist place and tells the attendee", async () => {
  let promoted = false;
  server({mine: () => json(200, {server_time: "x", revisions: {1: promoted ? "11:CONFIRMED" : "11:WAITLISTED"}, choices: [choice(1, promoted ? "confirmed" : "waitlisted", promoted ? null : 2)]})});
  render(<MasterclassProgram />);
  await screen.findByText("You are on the waitlist · place 2");
  promoted = true;
  fireEvent.click(screen.getByRole("button", {name: "Refresh programme"}));
  expect(await screen.findByText("Your seat was confirmed from the waitlist.")).toBeInTheDocument();
  expect(card(1).getByText("Your seat is confirmed")).toBeInTheDocument();
});

it("uses the observed revision and refreshes a conflict without retrying a write", async () => {
  let changed = false;
  const api = server({program: () => json(200, program([mc(1), mc(2)])),
    mine: () => json(200, {server_time:"x", revisions:{1: changed ? "12:CONFIRMED" : "11:CONFIRMED"}, choices:[choice(changed ? 2 : 1,"confirmed")]}),
    write: () => {changed=true;return failure(409,"MASTERCLASS_CHOICE_CONFLICT");}});
  render(<MasterclassProgram />);
  fireEvent.click(await (await findCard(2)).findByRole("button",{name:"Change my choice to this"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your choice changed on another device");
  await waitFor(()=>expect(card(2).getByText("Your seat is confirmed")).toBeInTheDocument());
  expect(api.writes()).toHaveLength(1);
  expect(api.writes()[0]?.body).toMatchObject({expected_revision:"11:CONFIRMED"});
});

it("polls only a visible waiting page and cleans up its timer", async () => {
  const intervals = vi.spyOn(window,"setInterval");
  const cleared = vi.spyOn(window,"clearInterval");
  const visibility = vi.spyOn(document,"visibilityState","get").mockReturnValue("visible");
  let reads=0;
  server({mine:()=>{reads++;return json(200,{server_time:"x",revisions:{1:"11:WAITLISTED"},choices:[choice(1,"waitlisted",1)]});}});
  const view=render(<MasterclassProgram />);
  await screen.findByText("You are on the waitlist · place 1");
  const tick=intervals.mock.calls.find(call=>call[1]===15000)?.[0] as ()=>void;
  expect(tick).toBeTypeOf("function");
  const start=reads;
  visibility.mockReturnValue("hidden");
  await act(async()=>tick());expect(reads).toBe(start);
  visibility.mockReturnValue("visible");
  await act(async()=>tick());expect(reads).toBe(start+1);
  await act(async()=>document.dispatchEvent(new Event("visibilitychange")));expect(reads).toBe(start+2);
  view.unmount();expect(cleared).toHaveBeenCalled();
  visibility.mockRestore();intervals.mockRestore();cleared.mockRestore();
});

it("does not overwrite a completed write with an older refresh",async()=>{
  let reads=0;let release:(r:Response)=>void=()=>{};
  const fetchMock=vi.fn(async(url:string,init?:RequestInit)=>{
    if(init?.method==="PUT")return result("confirmed",[choice(1,"confirmed")]);
    if(url==="/api/masterclasses")return json(200,program([mc(1)]));
    if(++reads===1)return json(200,{server_time:"x",revisions:{},choices:[]});
    return new Promise<Response>(resolve=>{release=resolve;});
  });vi.stubGlobal("fetch",fetchMock);
  render(<MasterclassProgram />);await screen.findByRole("button",{name:"Choose"});
  fireEvent.click(screen.getByRole("button",{name:"Refresh programme"}));
  fireEvent.click(card(1).getByRole("button",{name:"Choose"}));
  await screen.findByText("Your seat is confirmed.");
  await act(async()=>release(json(200,{server_time:"old",revisions:{},choices:[]})));
  expect(card(1).getByRole("button",{name:"Cancel"})).toBeInTheDocument();
});


it("keeps the dialog's observed revision when another device changes the choice", async () => {
  let changed = false;
  const api = server({
    program: () => json(200, program([mc(1), mc(2, {availability:"waitlist",seats_left:0})])),
    mine: () => json(200, {server_time:"x",revisions:{1:changed?"12:CONFIRMED":"11:CONFIRMED"},choices:[choice(1,"confirmed")]}),
    write: () => failure(409,"MASTERCLASS_CHOICE_CONFLICT"),
  });
  render(<MasterclassProgram />);
  fireEvent.click(await (await findCard(2)).findByRole("button", {name:"Change and join the waitlist"}));
  changed = true;
  fireEvent.click(screen.getByRole("button", {name:"Refresh programme"}));
  await waitFor(() => expect(screen.getByRole("button", {name:"Refresh programme"})).toBeEnabled());
  fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", {name:"Give up my seat and join the waitlist"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("another device");
  expect(api.writes()).toHaveLength(1);
  expect(api.writes()[0]?.body).toMatchObject({expected_revision:"11:CONFIRMED",give_up_confirmed_seat:true});
});

it("stops automatic waitlist refresh after an error and allows a manual recovery", async () => {
  const intervals = vi.spyOn(window,"setInterval");
  const cleared = vi.spyOn(window,"clearInterval");
  const visibility = vi.spyOn(document,"visibilityState","get").mockReturnValue("visible");
  let fail = false;
  server({mine:()=>fail?failure(503,"MAINTENANCE"):json(200,{server_time:"x",revisions:{1:"11:WAITLISTED"},choices:[choice(1,"waitlisted",1)]})});
  const view = render(<MasterclassProgram />);
  await screen.findByText("You are on the waitlist · place 1");
  // The refresh timer is installed by an effect after that render commits: wait
  // for it instead of assuming the effect already ran.
  await waitFor(() => expect(intervals.mock.calls.some(c=>c[1]===15000)).toBe(true));
  const tick = intervals.mock.calls.find(c=>c[1]===15000)?.[0] as ()=>void;
  fail=true;
  await act(async()=>tick());
  expect(await screen.findByRole("alert")).toHaveTextContent("Your choices could not be loaded");
  expect(cleared).toHaveBeenCalled();
  fail=false;
  fireEvent.click(screen.getByRole("button",{name:"Refresh programme"}));
  expect(await screen.findByText("You are on the waitlist · place 1")).toBeInTheDocument();
  view.unmount();visibility.mockRestore();intervals.mockRestore();cleared.mockRestore();
});
