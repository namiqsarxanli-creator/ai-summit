import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuditLog } from "./AuditLog";
import type { AuditEvent, AuditResult } from "@/lib/auditView";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const NO_RESULT: AuditResult = {
  kind: null, outcome: null, reason: null, error_class: null, rows_expected: null, rows_written: null,
  rows_matched: null, max_rows: null, bytes_written: null, elapsed_ms: null, snapshot_at: null, filters_used: [],
};

function event(id: number, patch: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id,
    created_at: "2026-09-01T10:00:00Z",
    actor_id: 7,
    actor_name: "Admin Person",
    action: "booth.update",
    target_type: "booth",
    target_id: String(id),
    operation_id: null,
    before: { name: `Old ${id}`, is_active: true },
    after: { name: `New ${id}`, is_active: true },
    changed_fields: ["name"],
    result: null,
    hidden_field_count: 0,
    details: "known",
    ...patch,
  };
}

function page(events: AuditEvent[], next: string | null): Response {
  return jsonResponse(200, { items: events, next_cursor: next, limit: 50 });
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
  if (cursor === null) return page([event(1), event(2)], "c2");
  if (cursor === "c2") return page([event(3), event(4)], "c3");
  return page([event(5)], null);
};

function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((r) => { resolve = r; });
  return { promise, resolve };
}

describe("AuditLog — what a row shows", () => {
  it("asks the audit endpoint, then shows the action, actor, target and time of each event", async () => {
    const api = server(() => page([event(1, { actor_name: "Ada Admin", actor_id: 3 })], null));
    render(<AuditLog refreshKey={0} />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading audit events…");
    const list = await screen.findByRole("list", { name: "Audit events" });
    expect(api.calls[0]).toBe("/api/admin/audit");
    const row = within(list).getByRole("listitem");
    expect(row).toHaveTextContent("Booth updated"); // the kind, not the raw code
    expect(row).toHaveTextContent("Ada Admin (#3)");
    expect(row).toHaveTextContent("Booth #1");
    expect(row.querySelector("time")).toHaveAttribute("datetime", "2026-09-01T10:00:00Z");
    // the time is the viewer's local time, formatted - not the ISO text
    expect(row.querySelector("time")?.textContent).toBe(
      new Date("2026-09-01T10:00:00Z").toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" }),
    );
    expect(row).not.toHaveTextContent("2026-09-01T10:00:00Z");
    expect(screen.getByText(/Times are shown in your local time zone/)).toHaveTextContent(Intl.DateTimeFormat().resolvedOptions().timeZone);
  });

  it("shows an unknown kind of event by its code and says only the metadata is known", async () => {
    server(() => page([event(1, { action: "weird.new_thing", target_type: "gadget", details: "limited", before: null, after: null, changed_fields: [], hidden_field_count: 3 })], null));
    render(<AuditLog refreshKey={0} />);
    const row = (await screen.findByRole("listitem"));
    expect(row).toHaveTextContent("weird.new_thing");
    expect(row).toHaveTextContent("gadget #1");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(row).toHaveTextContent("only who, when and what it concerned are shown");
    expect(row).toHaveTextContent("Some fields are hidden because they are personal or not needed here: 3");
    expect(within(row).queryByRole("table")).not.toBeInTheDocument(); // no before/after to show
  });
});

describe("AuditLog — details and the before/after difference", () => {
  it("opens and closes the details; the changed field is marked and the unchanged one is not", async () => {
    server(() => page([event(1, { before: { name: "Old hall", is_active: true }, after: { name: "New hall", is_active: true } })], null));
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    const toggle = within(row).getByRole("button", { name: "Details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(row).queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent("Hide details");
    const table = within(row).getByRole("table");
    const name = within(table).getByRole("row", { name: /Name/ });
    expect(name).toHaveTextContent("Changed");
    expect(name).toHaveTextContent("Old hall");
    expect(name).toHaveTextContent("New hall");
    const active = within(table).getByRole("row", { name: /Active/ });
    expect(active).not.toHaveTextContent("Changed");
    expect(active).toHaveTextContent("Yes");

    fireEvent.click(toggle);
    expect(within(row).queryByRole("table")).not.toBeInTheDocument();
  });

  it("a creation has no 'before'; a field only one side recorded reads 'Not recorded'", async () => {
    server(() => page([event(1, { action: "booth.weight", target_type: "booth_interest", before: { weight: "1.00" }, after: { weight: "0.50", booth_id: 2 }, changed_fields: ["weight"] })], null));
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(within(row).getByRole("row", { name: /Booth ID/ })).toHaveTextContent("Not recorded");
    expect(within(row).getByRole("row", { name: /Weight/ })).toHaveTextContent("1.00");
  });

  it("says how many fields the server held back, without naming any", async () => {
    server(() => page([event(1, { action: "staff.create", target_type: "staff_account", before: null, after: { id: 9, role: "BOOTH_STAFF", booth_id: 2, is_active: true }, changed_fields: ["booth_id", "id", "is_active", "role"], hidden_field_count: 2 })], null));
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(row).toHaveTextContent("Some fields are hidden because they are personal or not needed here: 2");
    expect(within(row).getByRole("row", { name: /Role/ })).toHaveTextContent("Booth staff");
    expect(row).not.toHaveTextContent(/e-?mail/i);
  });

  it("shows the outcome and counts of an export, and does not claim the file was saved", async () => {
    const finished = event(2, {
      action: "export.finished", target_type: "export", target_id: "0123456789abcdef", operation_id: "0123456789abcdef",
      before: null, after: null, changed_fields: [],
      result: { ...NO_RESULT, kind: "attendees", outcome: "stream_finished", rows_expected: 1200, rows_written: 1200, bytes_written: 90000, elapsed_ms: 41, filters_used: ["q", "status"] },
    });
    const interrupted = event(3, {
      action: "export.finished", target_type: "export", target_id: "fedcba9876543210", operation_id: "fedcba9876543210",
      before: null, after: null, changed_fields: [],
      result: { ...NO_RESULT, kind: "points-log", outcome: "interrupted", reason: "closed_before_end", rows_expected: 900, rows_written: 120 },
    });
    server(() => page([finished, interrupted], null));
    render(<AuditLog refreshKey={0} />);
    const list = await screen.findByRole("list", { name: "Audit events" });
    const [first, second] = within(list).getAllByRole("listitem");

    // the row already says how it went
    expect(first).toHaveTextContent("CSV export finished");
    expect(first).toHaveTextContent("The server sent the whole file · 1,200 / 1,200 rows");
    expect(second).toHaveTextContent("Interrupted before the end · 120 / 900 rows");

    fireEvent.click(within(first!).getByRole("button", { name: "Details" }));
    expect(first).toHaveTextContent("Search, Status"); // the NAMES of the filters used
    expect(first).toHaveTextContent("Operation ID");
    expect(first).toHaveTextContent("0123456789abcdef");
    expect(first).toHaveTextContent("The server records what it sent, not whether the browser saved the file.");
    expect(first).not.toHaveTextContent(/was saved|saved to disk|downloaded successfully/i);

    fireEvent.click(within(second!).getByRole("button", { name: "Details" }));
    expect(second).toHaveTextContent("The download was closed before the end");
    expect(second).not.toHaveTextContent("The server records what it sent"); // only said for a finished stream
  });

  it("'Show the events of this export' filters the list to that operation and starts at page 1", async () => {
    const op = "0123456789abcdef";
    const started = event(10, { action: "export.started", target_type: "export", target_id: op, operation_id: op, before: null, after: null, changed_fields: [], result: { ...NO_RESULT, kind: "attendees", outcome: "started", rows_expected: 5 } });
    const finished = event(11, { action: "export.finished", target_type: "export", target_id: op, operation_id: op, before: null, after: null, changed_fields: [], result: { ...NO_RESULT, kind: "attendees", outcome: "stream_finished", rows_expected: 5, rows_written: 5 } });
    const api = server((params) => (params.get("target_id") === op ? page([finished, started], null) : page([finished], "next-1")));
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));

    fireEvent.click(within(row).getByRole("button", { name: "Show the events of this export" }));

    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(2));
    const last = api.params().at(-1)!;
    expect(last.get("target_type")).toBe("export");
    expect(last.get("target_id")).toBe(op);
    expect(last.get("cursor")).toBeNull();
    expect(screen.getByLabelText("Target type")).toHaveValue("export");
    expect(screen.getByLabelText("Target ID")).toHaveValue(op);
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();
    // both events of the operation are there; once filtered, the button is no longer offered
    expect(within(screen.getByRole("list", { name: "Audit events" })).getAllByText(/CSV export (started|finished)/)).toHaveLength(2);
    fireEvent.click(within(screen.getAllByRole("listitem")[0]!).getByRole("button", { name: "Details" }));
    expect(screen.getByText("This export's events are shown.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show the events of this export" })).not.toBeInTheDocument();
  });
});

describe("AuditLog — codes the page does not know", () => {
  it("shows an unknown outcome or reason code as plain text, never looking it up on the object's prototype", async () => {
    server(() => page([event(1, {
      action: "export.finished", target_type: "export", target_id: "abc", operation_id: "abc", before: null, after: null, changed_fields: [],
      result: { ...NO_RESULT, outcome: "stream_finished", reason: "constructor", error_class: "ValueError" },
    })], null));
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(row).toHaveTextContent("constructor");
    expect(row).toHaveTextContent("ValueError");
  });
});

describe("AuditLog — markup in the data is shown as text", () => {
  it("renders HTML in names, targets and values as plain text, and creates no elements from it", async () => {
    const attack = `<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>`;
    server(() => page([event(1, {
      actor_name: attack, target_type: attack, target_id: attack, action: attack,
      before: { name: attack }, after: { name: `${attack}!` }, changed_fields: ["name"],
    })], null));
    const { container } = render(<AuditLog refreshKey={0} />);
    const row = await screen.findByRole("listitem");
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(row.textContent).toContain(attack); // visible, as text
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
  });
});

describe("AuditLog — paging", () => {
  it("walks forward with the server's cursors and back by re-reading the earlier page", async () => {
    const api = server(threePages);
    render(<AuditLog refreshKey={0} />);

    expect(await screen.findByText("Booth #1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Booth #3")).toBeInTheDocument();
    expect(screen.queryByText("Booth #1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Booth #5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled(); // no next_cursor: the end
    expect(screen.getByText(/Page\s*3/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Booth #3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Booth #1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();

    expect(api.params().map((p) => p.get("cursor"))).toEqual([null, "c2", "c3", "c2", null]);
  });

  it("a double click on Next moves one page, not two", async () => {
    const api = server(threePages);
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");
    const next = screen.getByRole("button", { name: "Next" });
    await act(async () => {
      next.click();
      next.click();
    });
    await screen.findByText("Booth #3");
    expect(screen.getByText(/Page\s*2/)).toBeInTheDocument();
    expect(api.params().map((p) => p.get("cursor"))).toEqual([null, "c2"]);
  });

  it("an open detail closes when the page changes", async () => {
    server(threePages);
    render(<AuditLog refreshKey={0} />);
    const row = await screen.findByText("Booth #1").then((el) => el.closest("li") as HTMLElement);
    fireEvent.click(within(row).getByRole("button", { name: "Details" }));
    expect(within(row).getByRole("table")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Booth #3");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("AuditLog — filters", () => {
  it("applying filters sends the API's names, dates as timezone-aware instants, and starts at page 1", async () => {
    const api = server((params) => (params.get("action") ? page([event(9)], null) : threePages(params)));
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Booth #3");

    fireEvent.change(screen.getByLabelText("Staff ID"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "booth.update" } });
    fireEvent.change(screen.getByLabelText("Target type"), { target: { value: "booth" } });
    fireEvent.change(screen.getByLabelText("Target ID"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("Until date"), { target: { value: "2026-09-02" } });
    expect(api.calls).toHaveLength(2); // typing alone sends nothing
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Booth #9")).toBeInTheDocument();
    const last = api.params().at(-1)!;
    expect(last.get("actor_id")).toBe("12");
    expect(last.get("action")).toBe("booth.update");
    expect(last.get("target_type")).toBe("booth");
    expect(last.get("target_id")).toBe("5");
    expect(last.get("cursor")).toBeNull(); // c2 belonged to the old filter
    // the start of the first local day, up to the start of the day AFTER the last
    expect(last.get("created_from")).toBe(new Date("2026-09-01T00:00:00").toISOString());
    expect(last.get("created_to")).toBe(new Date("2026-09-03T00:00:00").toISOString());
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("Reset clears the fields and the filters and reloads the unfiltered first page", async () => {
    const api = server((params) => (params.get("action") ? page([event(9)], null) : threePages(params)));
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "task.update" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Booth #9");

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));

    await screen.findByText("Booth #1");
    expect(screen.getByLabelText("Action")).toHaveValue("");
    expect(api.params().at(-1)!.has("action")).toBe(false);
  });

  it("refuses an impossible filter on the page, without asking the server", async () => {
    const api = server(threePages);
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");

    fireEvent.change(screen.getByLabelText("Target ID"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a target type to search by target ID.");

    fireEvent.change(screen.getByLabelText("Target type"), { target: { value: "booth" } });
    fireEvent.change(screen.getByLabelText("Target ID"), { target: { value: "5 OR 1=1" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("The target ID may only contain");

    fireEvent.change(screen.getByLabelText("Target ID"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Staff ID"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole number greater than zero.");

    fireEvent.change(screen.getByLabelText("Staff ID"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2026-09-05" } });
    fireEvent.change(screen.getByLabelText("Until date"), { target: { value: "2026-09-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByRole("alert")).toHaveTextContent("The start date must not be after the end date.");

    expect(api.calls).toHaveLength(1); // only the first load
  });
});

describe("AuditLog — late answers", () => {
  it("the slow answer to an old filter is dropped when the new filter already answered", async () => {
    const slow = deferred();
    const api = server((params) => {
      if (params.get("action") === "booth.update") return slow.promise;
      if (params.get("action") === "task.update") return page([event(2, { action: "task.update", target_type: "task", before: null, after: null, changed_fields: [] })], null);
      return page([event(1)], null);
    });
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");

    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "booth.update" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "task.update" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Task #2")).toBeInTheDocument();

    await act(async () => { slow.resolve(page([event(66, { actor_name: "Late Answer" })], null)); });

    expect(screen.queryByText(/Late Answer/)).not.toBeInTheDocument();
    expect(screen.getByText("Task #2")).toBeInTheDocument();
    expect(api.calls).toHaveLength(3);
  });

  it("the slow answer for page 2 is dropped when Reset already answered with page 1", async () => {
    const slow = deferred();
    server((params) => (params.get("cursor") === "c2" ? slow.promise : params.get("cursor") === null ? page([event(1)], "c2") : page([], null)));
    render(<AuditLog refreshKey={0} />);
    await screen.findByText("Booth #1");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(await screen.findByText("Booth #1")).toBeInTheDocument();

    await act(async () => { slow.resolve(page([event(77, { actor_name: "Stale Page Two" })], null)); });

    expect(screen.queryByText(/Stale Page Two/)).not.toBeInTheDocument();
    expect(screen.getByText(/Page\s*1/)).toBeInTheDocument();
  });

  it("a refresh wins over an older request that answers later", async () => {
    const slow = deferred();
    let calls = 0;
    server(() => {
      calls += 1;
      return calls === 1 ? slow.promise : page([event(2, { actor_name: "Fresh" })], null);
    });
    const { rerender } = render(<AuditLog refreshKey={0} />);
    rerender(<AuditLog refreshKey={1} />);
    expect(await screen.findByText(/Fresh/)).toBeInTheDocument();

    await act(async () => { slow.resolve(page([event(1, { actor_name: "Stale" })], null)); });

    expect(screen.queryByText(/Stale/)).not.toBeInTheDocument();
    expect(screen.getByText(/Fresh/)).toBeInTheDocument();
  });

  it("an answer arriving after the section was left changes nothing and raises no error", async () => {
    const slow = deferred();
    server(() => slow.promise);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = render(<AuditLog refreshKey={0} />);
    unmount();
    await act(async () => { slow.resolve(page([event(1)], null)); });
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});

describe("AuditLog — states", () => {
  it("shows loading, then an empty message that depends on whether a filter is active", async () => {
    server((params) => page([], params.get("action") ? null : null));
    render(<AuditLog refreshKey={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading audit events…");
    expect(await screen.findByText("No audit events recorded yet.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "task.update" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("No audit events match these filters.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Audit events" })).not.toBeInTheDocument();
  });

  it("shows an error with a way to retry", async () => {
    let fail = true;
    const api = server(() => (fail ? errorResponse(500, "INTERNAL_ERROR") : page([event(1)], null)));
    render(<AuditLog refreshKey={0} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Audit events" })).not.toBeInTheDocument();

    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Booth #1")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(api.calls).toHaveLength(2);
  });

  it("explains a rejected filter, an ended session and a missing permission", async () => {
    server(() => errorResponse(422, "VALIDATION_ERROR"));
    const first = render(<AuditLog refreshKey={0} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The filters are not valid");
    first.unmount();

    server(() => errorResponse(401, "SESSION_REQUIRED"));
    const second = render(<AuditLog refreshKey={0} />);
    expect((await screen.findByRole("alert")).textContent).not.toBe("");
    second.unmount();

    server(() => errorResponse(403, "STAFF_NOT_AUTHORIZED"));
    render(<AuditLog refreshKey={0} />);
    expect((await screen.findByRole("alert")).textContent).not.toBe("");
  });

  it("an answer without a list of items is treated as an error, not rendered", async () => {
    server(() => jsonResponse(200, { nonsense: true }));
    render(<AuditLog refreshKey={0} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Audit events" })).not.toBeInTheDocument();
  });
});
