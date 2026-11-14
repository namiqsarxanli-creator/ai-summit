import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AdminList } from "./AdminList";

type Row = Record<string, unknown>;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function page(ids: number[], next: string | null): Response {
  return jsonResponse(200, {
    items: ids.map((id): Row => ({ id, first_name: `Person${id}`, email: `p${id}@x.test`, created_at: "2026-09-01T10:00:00Z" })),
    next_cursor: next,
    limit: 50,
  });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

afterEach(() => vi.unstubAllGlobals());

// A fetch whose answer is chosen per request by `answer(searchParams)`.
function server(answer: (params: URLSearchParams, path: string) => Response | Promise<Response>) {
  const calls: string[] = [];
  const mock = vi.fn(async (url: string) => {
    calls.push(url);
    const parsed = new URL(url, "http://local.test");
    return answer(parsed.searchParams, parsed.pathname);
  });
  vi.stubGlobal("fetch", mock);
  return { calls, params: () => calls.map((c) => new URL(c, "http://local.test").searchParams) };
}

const threePages = (params: URLSearchParams): Response => {
  const cursor = params.get("cursor");
  if (cursor === null) return page([1, 2], "c2");
  if (cursor === "c2") return page([3, 4], "c3");
  return page([5], null);
};

describe("AdminList — paging", () => {
  it("walks forward with the server's cursors and back by re-reading the earlier page", async () => {
    const api = server(threePages);
    render(<AdminList kind="attendees" refreshKey={0} />);

    expect(await screen.findByText("Person1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Person3")).toBeInTheDocument();
    expect(screen.queryByText("Person1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Person5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled(); // no next_cursor: the end
    expect(screen.getByText(/Page\s*3/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Person3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Person1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

    expect(api.params().map((p) => p.get("cursor"))).toEqual([null, "c2", "c3", "c2", null]);
  });

  it("a double click on Next moves one page, not two", async () => {
    const api = server(threePages);
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");
    const next = screen.getByRole("button", { name: "Next" });
    await act(async () => {
      next.click();
      next.click();
    });
    await screen.findByText("Person3");
    expect(screen.getByText(/Page\s*2/)).toBeInTheDocument();
    expect(api.params().map((p) => p.get("cursor"))).toEqual([null, "c2"]);
  });
});

describe("AdminList — filters", () => {
  it("applying a filter starts again at page 1 and forgets the cursor history", async () => {
    const api = server((params) => (params.get("q") === "ada" ? page([9], null) : threePages(params)));
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Person3");

    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Person9")).toBeInTheDocument();
    const last = api.params().at(-1)!;
    expect(last.get("q")).toBe("ada");
    expect(last.get("cursor")).toBeNull(); // c2 belonged to the old filter
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("typing alone sends nothing until Search; Reset clears fields and filters", async () => {
    const api = server((params) => (params.get("status") ? page([7], null) : threePages(params)));
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");

    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "bo" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "ACTIVE" } });
    expect(api.calls).toHaveLength(1); // still only the first load

    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Person7");
    expect(api.params().at(-1)!.get("status")).toBe("ACTIVE");
    expect(api.params().at(-1)!.get("q")).toBe("bo");

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await screen.findByText("Person1");
    expect(screen.getByLabelText("Search name or e-mail")).toHaveValue("");
    expect(screen.getByLabelText("Status")).toHaveValue("");
    const reloaded = api.params().at(-1)!;
    expect([...reloaded.keys()]).toEqual([]); // no filter left, back to the first page
  });

  it("sends dates as timezone-aware instants: the start of the first day up to the start of the day after the last", async () => {
    const api = server(() => page([1], null));
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");
    fireEvent.change(screen.getByLabelText("Registered from"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("Registered until"), { target: { value: "2026-09-02" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.calls).toHaveLength(2));

    const sent = api.params().at(-1)!;
    expect(sent.get("registered_from")).toBe(new Date("2026-09-01T00:00:00").toISOString());
    expect(sent.get("registered_to")).toBe(new Date("2026-09-03T00:00:00").toISOString());
    expect(sent.get("registered_from")).toMatch(/Z$/); // an explicit UTC instant, never a bare local time
  });

  it("refuses an impossible filter on the page, without asking the server", async () => {
    const api = server(() => page([1], null));
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");

    fireEvent.change(screen.getByLabelText("Registered from"), { target: { value: "2026-09-05" } });
    fireEvent.change(screen.getByLabelText("Registered until"), { target: { value: "2026-09-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByRole("alert")).toHaveTextContent("The start date must not be after the end date.");
    expect(api.calls).toHaveLength(1);
  });

  it("points ledger: the filter names are the API's, and an id must be a positive whole number", async () => {
    const api = server(() => page([1], null));
    render(<AdminList kind="points-log" refreshKey={0} />);
    await screen.findByText("Person1");

    fireEvent.change(screen.getByLabelText("Attendee ID"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole number greater than zero.");
    expect(api.calls).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Attendee ID"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Booth ID"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Activity ID"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Staff ID"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "TASK" } });
    fireEvent.change(screen.getByLabelText("Entry type"), { target: { value: "AWARD" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.calls).toHaveLength(2));

    expect(api.calls[1]!.startsWith("/api/admin/points-log?")).toBe(true);
    expect(Object.fromEntries(api.params()[1]!)).toEqual({
      user_id: "12", booth_id: "3", task_id: "4", staff_id: "5", source: "TASK", entry_type: "AWARD",
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("AdminList — a late answer never replaces a newer one", () => {
  function deferred() {
    let resolve!: (value: Response) => void;
    const promise = new Promise<Response>((r) => { resolve = r; });
    return { promise, resolve };
  }

  it("the slow answer to an old filter is dropped when the new filter already answered", async () => {
    const slow = deferred();
    server((params) => (params.get("q") === "new" ? page([2], null) : slow.promise));
    render(<AdminList kind="attendees" refreshKey={0} />);

    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Person2")).toBeInTheDocument();

    await act(async () => { slow.resolve(page([1], null)); }); // the OLD request finally answers
    expect(screen.getByText("Person2")).toBeInTheDocument();
    expect(screen.queryByText("Person1")).not.toBeInTheDocument();
  });

  it("the slow answer for page 2 of the old filter is dropped when a new filter already answered", async () => {
    const slow = deferred();
    server((params) => {
      if (params.get("q") === "new") return page([8], null);
      return params.get("cursor") === "c2" ? slow.promise : page([1], "c2");
    });
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");

    fireEvent.click(screen.getByRole("button", { name: "Next" })); // page 2 of the old filter: slow
    expect(screen.getByRole("status")).toHaveTextContent("Loading records…");
    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Person8")).toBeInTheDocument();

    await act(async () => { slow.resolve(page([3], "c3")); }); // page 2 of the OLD filter lands now
    expect(screen.getByText("Person8")).toBeInTheDocument();
    expect(screen.queryByText("Person3")).not.toBeInTheDocument();
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();
  });

  it("a refresh after a mutation wins over an older request that answers later", async () => {
    const first = deferred();
    let calls = 0;
    server(() => {
      calls += 1;
      return calls === 1 ? first.promise : page([2], null);
    });
    const { rerender } = render(<AdminList kind="attendees" refreshKey={0} />);
    rerender(<AdminList kind="attendees" refreshKey={1} />); // the refresh
    expect(await screen.findByText("Person2")).toBeInTheDocument();

    await act(async () => { first.resolve(page([1], null)); }); // the older request answers last
    expect(screen.getByText("Person2")).toBeInTheDocument();
    expect(screen.queryByText("Person1")).not.toBeInTheDocument();
  });

  it("an answer arriving after the list was left changes nothing and raises no error", async () => {
    const slow = deferred();
    server(() => slow.promise);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const view = render(<AdminList kind="attendees" refreshKey={0} />);
    view.unmount();
    await act(async () => { slow.resolve(page([1], null)); });
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});

describe("AdminList — states", () => {
  it("shows loading, then an empty message that depends on whether a filter is active", async () => {
    server(() => page([], null));
    render(<AdminList kind="attendees" refreshKey={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading records…");
    expect(await screen.findByText("No records to show yet.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "nobody" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("No records match these filters.")).toBeInTheDocument();
  });

  it("shows an error with a way to retry, and a clear message for a rejected filter", async () => {
    let fail = true;
    server(() => (fail ? errorResponse(500, "INTERNAL_ERROR") : page([1], null)));
    render(<AdminList kind="attendees" refreshKey={0} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on our side.");
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();

    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Person1")).toBeInTheDocument();
  });

  it("explains a server-side validation failure in terms of the filters", async () => {
    server(() => errorResponse(422, "VALIDATION_ERROR"));
    render(<AdminList kind="points-log" refreshKey={0} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The filters are not valid.");
  });

  it("an answer without a list of items is treated as an error, not rendered", async () => {
    server(() => jsonResponse(200, []));
    render(<AdminList kind="attendees" refreshKey={0} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("renders timestamps in the viewer's locale rather than as raw ISO text", async () => {
    server(() => page([1], null));
    render(<AdminList kind="attendees" refreshKey={0} />);
    const row = (await screen.findByText("Person1")).closest("li") as HTMLElement;
    expect(within(row).queryByText("2026-09-01T10:00:00Z")).not.toBeInTheDocument();
    expect(within(row).getByText(new Date("2026-09-01T10:00:00Z").toLocaleString())).toBeInTheDocument();
  });
});

function ledger(rows: Row[], next: string | null = null): Response {
  return jsonResponse(200, { items: rows, next_cursor: next, limit: 50 });
}
function award(id: number, patch: Row = {}): Row {
  return {
    id, user_id: 7, attendee_name: `Attendee ${id}`, booth_id: 1, booth_name: "Hall A", source: "TASK", entry_type: "AWARD",
    points: 10, task_id: 3, activity: "Try the demo", entered_by_staff_id: 2, staff_name: "Noah", entered_by_system: false,
    created_at: "2026-09-01T10:00:00Z", reverses_entry_id: null, reversed_by_entry_id: null, reason: null, ...patch,
  };
}

describe("AdminList — reversing points", () => {
  it("offers Reverse on an award nothing has reversed - and on nothing else", async () => {
    server(() => ledger([
      award(1),
      award(2, { reversed_by_entry_id: 9 }), // already reversed
      award(9, { entry_type: "REVERSAL", points: -10, reverses_entry_id: 2, reason: "Wrong person" }),
    ]));
    render(<AdminList kind="points-log" refreshKey={0} />);
    await screen.findByText("Attendee 1");
    expect(screen.getAllByRole("button", { name: "Reverse…" })).toHaveLength(1);
    const first = screen.getByText("Attendee 1").closest("li") as HTMLElement;
    expect(within(first).getByRole("button", { name: "Reverse…" })).toBeInTheDocument();
    // the link between an award and its reversal is on screen, both ways
    const reversed = screen.getByText("Attendee 2").closest("li") as HTMLElement;
    expect(reversed).toHaveTextContent("reversed by entry id");
    expect(reversed).toHaveTextContent("9");
    const reversal = screen.getByText("Attendee 9").closest("li") as HTMLElement;
    expect(reversal).toHaveTextContent("reverses entry id");
    expect(reversal).toHaveTextContent("Wrong person");
  });

  it("the attendee list has no Reverse button", async () => {
    server(() => page([1], null));
    render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Person1");
    expect(screen.queryByRole("button", { name: "Reverse…" })).not.toBeInTheDocument();
  });

  it("Reverse opens a confirmation screen INSTEAD of the list; Cancel returns to it without a request", async () => {
    const api = server(() => ledger([award(1), award(2)]));
    render(<AdminList kind="points-log" refreshKey={0} />);
    await screen.findByText("Attendee 1");
    const row = screen.getByText("Attendee 1").closest("li") as HTMLElement;

    fireEvent.click(within(row).getByRole("button", { name: "Reverse…" }));

    expect(screen.getByRole("heading", { name: "Reverse points" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Reason \(required/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Admin records" })).not.toBeInTheDocument(); // the list is not beside it
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findByText("Attendee 2")).toBeInTheDocument();
    expect(api.calls.filter((c) => c.includes("reverse"))).toHaveLength(0);
    expect(api.calls).toHaveLength(1); // not even a reload: nothing changed
  });

  it("after a successful reversal the list is re-read, with its filters and page kept", async () => {
    let reversed = false;
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/reverse")) {
        reversed = true;
        return jsonResponse(200, { reversal_id: 50, reversed_entry_id: 1, points_reversed: 10, resulting_total: 0, created_at: "2026-09-01T11:00:00Z", replayed: false });
      }
      return ledger(reversed ? [award(1, { reversed_by_entry_id: 50 })] : [award(1)]);
    }));
    render(<AdminList kind="points-log" refreshKey={0} />);
    await screen.findByText("Attendee 1");
    fireEvent.change(screen.getByLabelText("Attendee ID"), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(calls.filter((c) => c.startsWith("GET")).length).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: "Reverse…" }));
    fireEvent.change(screen.getByLabelText(/Reason \(required/), { target: { value: "Scanned the wrong attendee" } });
    fireEvent.click(screen.getByRole("button", { name: /^Reverse these points/ }));
    await screen.findByText("Points reversed");

    fireEvent.click(screen.getByRole("button", { name: "Back to the ledger" }));

    expect(await screen.findByText("Attendee 1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reverse…" })).not.toBeInTheDocument(); // it is reversed now
    expect(screen.getByLabelText("Attendee ID")).toHaveValue("7"); // the filter survived the detour
    const reads = calls.filter((c) => c.startsWith("GET"));
    expect(reads.at(-1)).toContain("user_id=7");
    expect(calls.filter((c) => c.includes("/reverse"))).toEqual(["POST /api/admin/points-log/1/reverse"]);
  });
});
