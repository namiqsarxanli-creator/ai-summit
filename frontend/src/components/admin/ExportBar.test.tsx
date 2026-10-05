import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminList } from "./AdminList";
import { GIVE_UP_AFTER_MS, MAX_STATUS_FAILURES, NOT_STARTED_AFTER_MS, POLL_MS } from "./ExportBar";
import { exportBody } from "./listFilters";

// These tests drive the page against a FAKE server (fetch is stubbed) and a fake
// clock. They show what the page does with the answers it is given. They are NOT
// a browser download test: whether a real browser saves the file is checked by hand.

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}
const listPage = () => jsonResponse(200, { items: [{ id: 1, first_name: "Ada" }], next_cursor: null, limit: 50 });

const DOWNLOAD = "/api/admin/exports/download?ticket=abc123";
let counter = 0;
const prepared = (rows = 12) => {
  counter += 1;
  return jsonResponse(200, {
    download_url: `${DOWNLOAD}${counter}`,
    ref: `ref${counter}`,
    status_url: `/api/admin/exports/status/ref${counter}`,
    kind: "attendees",
    rows,
    max_rows: 100000,
    expires_in_seconds: 60,
  });
};

type Status = {
  state: "ready" | "starting" | "streaming" | "finished" | "interrupted" | "failed" | "rejected" | "expired";
  code?: string | null;
  retry?: "none" | "same_link" | "new_export";
  rows_expected?: number | null;
  rows_written?: number | null;
};
const status = (s: Status) => jsonResponse(200, { ref: "r", kind: "attendees", code: null, retry: "none", rows_expected: null, rows_written: null, ...s });

type Call = { url: string; method: string; body: unknown };

// fetch: the list, POST /exports/<kind>, and GET /exports/status/<ref> answered by `onStatus`.
function server(opts: { onStatus?: (n: number, call: Call) => Response | Promise<Response>; onPrepare?: (call: Call) => Response | Promise<Response> } = {}) {
  const prepares: Call[] = [];
  const statusCalls: Call[] = [];
  const everything: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const call: Call = { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body as string) : undefined };
    everything.push(call);
    if (url.startsWith("/api/admin/exports/status/")) {
      statusCalls.push(call);
      return (opts.onStatus ?? (() => status({ state: "ready" })))(statusCalls.length, call);
    }
    if (url.startsWith("/api/admin/exports/")) {
      prepares.push(call);
      return (opts.onPrepare ?? (() => prepared()))(call);
    }
    return listPage();
  }));
  return { prepares, statusCalls, everything };
}

const frame = () => document.body.querySelector("iframe");
const frames = () => Array.from(document.body.querySelectorAll("iframe"));

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

async function open(kind: "attendees" | "points-log" = "attendees") {
  render(<AdminList kind={kind} refreshKey={0} />);
  await screen.findByText("Ada");
}

async function press(name: string) {
  await act(async () => { fireEvent.click(screen.getByRole("button", { name })); });
}

beforeEach(() => {
  counter = 0;
  frames().forEach((f) => f.remove());
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ExportBar — what is exported", () => {
  it("exports with the APPLIED filters, not what is merely typed in the form", async () => {
    const api = server();
    await open();
    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "ada" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "ACTIVE" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.everything.some((c) => c.url.includes("q=ada"))).toBe(true));

    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "someone else" } });
    expect(screen.getByText(/did not press Search/)).toBeInTheDocument();
    await press("Export CSV");

    expect(api.prepares[0]).toMatchObject({ url: "/api/admin/exports/attendees", method: "POST", body: { q: "ada", status: "ACTIVE" } });
    expect(Object.keys(api.prepares[0]!.body as object).sort()).toEqual(["q", "status"]); // no cursor, no page size
  });

  it("sends the same dates and typed values as the list does", async () => {
    const api = server();
    await open("points-log");
    fireEvent.change(screen.getByLabelText("Attendee ID"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "TASK" } });
    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("Until date"), { target: { value: "2026-09-02" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.everything.some((c) => c.url.includes("user_id=12"))).toBe(true));
    await press("Export CSV");

    const body = api.prepares[0]!.body as Record<string, unknown>;
    expect(api.prepares[0]!.url).toBe("/api/admin/exports/points-log");
    expect(body.user_id).toBe(12);
    expect(body.created_from).toBe(new Date("2026-09-01T00:00:00").toISOString());
    expect(body.created_to).toBe(new Date("2026-09-03T00:00:00").toISOString());
  });

  it("exportBody leaves out every empty field and any cursor or page size", () => {
    expect(exportBody("attendees", { q: "  ", status: "", sponsor_consent: "false" })).toEqual({ sponsor_consent: false });
    expect(exportBody("points-log", {})).toEqual({});
  });

  it("the sponsor export is its own, clearly labelled button, and sends no filters at all", async () => {
    const api = server();
    await open();
    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.getByText(/only active attendees who currently agree to share their details with sponsors/i)).toBeInTheDocument();
    await press("Export sponsor contacts");
    expect(api.prepares[0]).toMatchObject({ url: "/api/admin/exports/sponsor-contacts", method: "POST", body: {} });
  });

  it("the ledger has an export but no sponsor button", async () => {
    server();
    await open("points-log");
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export sponsor contacts" })).not.toBeInTheDocument();
  });
});

describe("ExportBar — what the page knows, and only that", () => {
  it("prepared -> download requested -> server reports its progress -> server finished; never 'saved'", async () => {
    const answers: Status[] = [
      { state: "ready" },
      { state: "starting" },
      { state: "streaming", rows_expected: 12, rows_written: 5 },
      { state: "finished", rows_expected: 12, rows_written: 12 },
    ];
    const api = server({ onStatus: (n) => status(answers[Math.min(n, answers.length) - 1]!) });
    const createObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL }));
    await open();

    await press("Export CSV");
    // 1. prepared: the download was requested, the server has not reported yet
    expect(screen.getByRole("status")).toHaveTextContent("Export prepared (about 12 rows). Waiting for the download to start…");
    expect(frame()?.getAttribute("src")).toBe(`${DOWNLOAD}1`);
    expect(frame()?.hidden).toBe(true);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();

    await tick(POLL_MS); // ready: still waiting
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the download to start");
    await tick(POLL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("The server is preparing the file…");
    await tick(POLL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("The server is sending the file: 5 of 12 rows…");
    await tick(POLL_MS);

    const done = screen.getByRole("status");
    expect(done).toHaveTextContent("The server finished sending the file (12 rows).");
    expect(done).toHaveTextContent("This page cannot tell whether your browser saved it. Check your downloads.");
    expect(done.textContent).not.toMatch(/downloaded|saved to|complete/i);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
    // the page never read the file itself
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(api.everything.some((c) => c.url.includes("/exports/download"))).toBe(false);
  });

  it("stops asking once the server has reported an end", async () => {
    const api = server({ onStatus: () => status({ state: "finished", rows_expected: 3, rows_written: 3 }) });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("finished sending");
    const asked = api.statusCalls.length;
    await tick(POLL_MS * 10);
    expect(api.statusCalls).toHaveLength(asked);
  });

  it("an empty result is announced as a header-only file", async () => {
    server({ onPrepare: () => prepared(0), onStatus: () => status({ state: "finished", rows_expected: 0, rows_written: 0 }) });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("The server finished sending the file (header row only).");
  });

  it("'ready' for too long means the browser never asked for the file: say so, and offer the SAME link again", async () => {
    const api = server();
    await open();
    await press("Export CSV");
    await tick(NOT_STARTED_AFTER_MS + POLL_MS);

    expect(screen.getByRole("alert")).toHaveTextContent("The browser did not start the download.");
    expect(screen.queryByText(/finished sending/)).not.toBeInTheDocument();
    const before = frame()!.getAttribute("src");
    await press("Try the download again");

    expect(api.prepares).toHaveLength(1); // no new export: the ticket is still the one issued
    expect(frame()!.getAttribute("src")).toBe(before);
    expect(screen.getByRole("status")).toHaveTextContent("The server is preparing the file…");
  });
});

describe("ExportBar — a refused or broken download is shown in the main page", () => {
  const cases: [string, Status, string, "Try the download again" | "Export again" | null][] = [
    ["a used / expired ticket (410)", { state: "rejected", code: "EXPORT_TICKET_INVALID", retry: "new_export" }, "This export link has expired or was already used. Start the export again.", "Export again"],
    ["a ticket that timed out unused", { state: "expired", code: "EXPORT_TICKET_INVALID", retry: "new_export" }, "This export link has expired or was already used.", "Export again"],
    ["another export running (409)", { state: "rejected", code: "EXPORT_BUSY", retry: "same_link" }, "An export is already running for your account. Wait for it to finish, then try the download again.", "Try the download again"],
    ["a result over the size limit (413)", { state: "rejected", code: "EXPORT_TOO_LARGE", retry: "new_export" }, "This export is too large. Narrow the filters and try again.", "Export again"],
    ["a start-up failure (503)", { state: "rejected", code: "EXPORT_FAILED", retry: "new_export" }, "The export could not be completed.", "Export again"],
    ["an unexpected server error (500)", { state: "rejected", code: "INTERNAL_ERROR", retry: "new_export" }, "Something went wrong on our side.", "Export again"],
    ["maintenance (503)", { state: "rejected", code: "MAINTENANCE", retry: "same_link" }, "The event system is briefly unavailable.", "Try the download again"],
  ];

  it.each(cases)("%s", async (_name, answer, text, retryLabel) => {
    const api = server({ onStatus: () => status(answer) });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);

    expect(screen.getByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText(/finished sending/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
    const retryNames = ["Try the download again", "Export again"];
    for (const name of retryNames) {
      if (name === retryLabel) expect(screen.getByRole("button", { name })).toBeInTheDocument();
      else expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(api.prepares).toHaveLength(1);
  });

  it("a retry that needs a NEW ticket prepares a new export with the same filters", async () => {
    const api = server({ onStatus: (n) => status(n === 1 ? { state: "rejected", code: "EXPORT_TICKET_INVALID", retry: "new_export" } : { state: "starting" }) });
    await open();
    fireEvent.change(screen.getByLabelText("Search name or e-mail"), { target: { value: "ada" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.everything.some((c) => c.url.includes("q=ada"))).toBe(true));
    await press("Export CSV");
    await tick(POLL_MS);
    await press("Export again");

    expect(api.prepares).toHaveLength(2);
    expect(api.prepares[1]!.body).toEqual(api.prepares[0]!.body);
    expect(frame()!.getAttribute("src")).toBe(`${DOWNLOAD}2`); // the NEW link
  });

  it("a retry that keeps the existing ticket re-points the frame at the same link and prepares nothing", async () => {
    const api = server({ onStatus: (n) => status(n === 1 ? { state: "rejected", code: "EXPORT_BUSY", retry: "same_link" } : { state: "streaming", rows_expected: 4, rows_written: 1 }) });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);
    const link = frame()!.getAttribute("src");
    await press("Try the download again");
    await tick(POLL_MS);

    expect(api.prepares).toHaveLength(1);
    expect(frame()!.getAttribute("src")).toBe(link);
    expect(screen.getByRole("status")).toHaveTextContent("The server is sending the file: 1 of 4 rows…");
  });

  it("a session that ended is reported plainly, with no retry that could not work", async () => {
    server({ onStatus: () => errorResponse(401, "UNAUTHENTICATED") });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);
    expect(screen.getByRole("alert")).toHaveTextContent("Your session has ended. Please sign in again.");
    expect(screen.queryByRole("button", { name: /again/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/finished sending/)).not.toBeInTheDocument();
  });

  it("a status the server no longer has is not guessed at", async () => {
    server({ onStatus: () => errorResponse(404, "ADMIN_NOT_FOUND") });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);
    expect(screen.getByRole("alert")).toHaveTextContent("This export's status is no longer available.");
    expect(screen.getByRole("button", { name: "Export again" })).toBeInTheDocument();
  });

  it("when the page cannot ask, it says the download may still be running and offers to check again", async () => {
    let reachable = false;
    const api = server({
      onStatus: () => {
        if (!reachable) throw new TypeError("offline");
        return status({ state: "finished", rows_expected: 2, rows_written: 2 });
      },
    });
    await open();
    await press("Export CSV");
    for (let i = 0; i < MAX_STATUS_FAILURES; i += 1) await tick(POLL_MS);

    expect(screen.getByRole("alert")).toHaveTextContent("We can't check the export's progress right now. The download may still be running.");
    reachable = true;
    await press("Check again");
    await tick(POLL_MS);
    expect(screen.getByRole("status")).toHaveTextContent("The server finished sending the file (2 rows).");
    expect(api.prepares).toHaveLength(1);
  });

  it("gives up honestly if the server never reports an end", async () => {
    server({ onStatus: () => status({ state: "streaming", rows_expected: 10, rows_written: 1 }) });
    await open();
    await press("Export CSV");
    await tick(GIVE_UP_AFTER_MS + POLL_MS * 2);
    expect(screen.getByRole("alert")).toHaveTextContent("This export's status is no longer available.");
  });
});

describe("ExportBar — a stream that started and then stopped", () => {
  it.each([["interrupted"], ["failed"]] as const)("%s: says the file is incomplete, with the counts, and offers a new export", async (state) => {
    server({ onStatus: () => status({ state, code: "EXPORT_ABORTED", retry: "new_export", rows_expected: 12, rows_written: 5 }) });
    await open();
    await press("Export CSV");
    await tick(POLL_MS);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The server stopped sending the file before the end, so the download is incomplete.");
    expect(alert).toHaveTextContent("5 of 12 rows were sent.");
    expect(screen.queryByText(/finished sending/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export again" })).toBeInTheDocument();
  });
});

describe("ExportBar — double clicks, newer exports, leaving the page", () => {
  it("a double click starts one export, and the buttons stay disabled until the server reports an end", async () => {
    const api = server({ onStatus: (n) => status(n < 3 ? { state: "streaming", rows_expected: 9, rows_written: n } : { state: "finished", rows_expected: 9, rows_written: 9 }) });
    await open();
    const button = screen.getByRole("button", { name: "Export CSV" });
    await act(async () => {
      button.click();
      button.click(); // a second click in the same tick
    });
    expect(api.prepares).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export sponsor contacts" })).toBeDisabled();

    await tick(POLL_MS);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeDisabled(); // still streaming
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(api.prepares).toHaveLength(1);

    await tick(POLL_MS * 2);
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
  });

  it("while the server prepares, the button says so and cannot be pressed again", async () => {
    let release!: (value: Response) => void;
    const api = server({ onPrepare: () => new Promise<Response>((resolve) => { release = resolve; }) });
    await open();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Export CSV" })); });
    expect(screen.getByRole("button", { name: "Preparing export…" })).toBeDisabled();
    await press("Preparing export…");
    expect(api.prepares).toHaveLength(1);
    await act(async () => { release(prepared()); });
    expect(screen.getByRole("status")).toHaveTextContent("Export prepared");
  });

  it("a status answer that arrives after the page was left changes nothing and raises no error", async () => {
    let releaseLate!: (value: Response) => void;
    server({ onStatus: () => new Promise<Response>((resolve) => { releaseLate = resolve; }) });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const view = render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Ada");
    await press("Export CSV");
    await tick(POLL_MS); // the first status request is now in flight and hangs
    view.unmount();
    await act(async () => { releaseLate(status({ state: "finished", rows_expected: 7, rows_written: 7 })); });
    await tick(POLL_MS * 3);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("leaving the page stops the polling", async () => {
    const api = server({ onStatus: () => status({ state: "streaming", rows_expected: 9, rows_written: 1 }) });
    const view = render(<AdminList kind="attendees" refreshKey={0} />);
    await screen.findByText("Ada");
    await press("Export CSV");
    await tick(POLL_MS);
    const asked = api.statusCalls.length;
    view.unmount();
    await tick(POLL_MS * 10);
    expect(api.statusCalls).toHaveLength(asked);
  });
});

describe("ExportBar — refusals while preparing", () => {
  it.each([
    [413, "EXPORT_TOO_LARGE", "This export is too large. Narrow the filters and try again.", "Export again"],
    [429, "RATE_LIMITED", "Too many attempts.", "Export again"],
    [422, "VALIDATION_ERROR", "The filters are not valid.", "Export again"],
    [503, "EXPORT_FAILED", "The export could not be completed.", "Export again"],
    [401, "UNAUTHENTICATED", "Your session has ended.", null],
  ])("explains a %i %s refusal, starts no download, and offers the right retry", async (code, name, text, retry) => {
    const api = server({ onPrepare: () => errorResponse(code, name) });
    await open();
    await press("Export CSV");

    expect(screen.getByRole("alert")).toHaveTextContent(text);
    expect(frames()).toHaveLength(0);
    expect(api.statusCalls).toHaveLength(0); // nothing to watch
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeEnabled();
    if (retry) expect(screen.getByRole("button", { name: retry })).toBeInTheDocument();
    else expect(screen.queryByRole("button", { name: /again/i })).not.toBeInTheDocument();
  });

  it("a network failure is an error, not a started download", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.startsWith("/api/admin/exports/")) throw new TypeError("offline");
      return listPage();
    }));
    await open();
    await press("Export CSV");
    expect(screen.getByRole("alert")).toHaveTextContent("We couldn't reach the server.");
    expect(frames()).toHaveLength(0);
  });
});
