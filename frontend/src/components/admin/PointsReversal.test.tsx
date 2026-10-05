import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PointsReversal } from "./PointsReversal";
import type { LedgerEntry } from "@/lib/reversal";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "server text", details: {}, correlation_id: "c" } });
}

const ENTRY: LedgerEntry = {
  id: 41, user_id: 7, attendee_name: "Ada Lovelace", booth_name: "Hall A", source: "TASK", entry_type: "AWARD",
  points: 10, activity: "Try the demo", created_at: "2026-09-01T10:00:00Z", reversed_by_entry_id: null,
};
const REVERSED = { reversal_id: 99, reversed_entry_id: 41, points_reversed: 10, resulting_total: 25, created_at: "2026-09-01T11:00:00Z", replayed: false };

afterEach(() => vi.unstubAllGlobals());

type Call = { url: string; body: { reason: string; idempotency_key: string } };

function server(answer: (call: Call, n: number) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const call = { url, body: JSON.parse(String(init?.body ?? "{}")) } as Call;
    calls.push(call);
    return answer(call, calls.length);
  }));
  return calls;
}

function open(entry: LedgerEntry = ENTRY) {
  const onClose = vi.fn();
  const view = render(<PointsReversal entry={entry} onClose={onClose} />);
  return { onClose, ...view };
}

function typeReason(text: string) {
  fireEvent.change(screen.getByLabelText(/Reason \(required/), { target: { value: text } });
}
const reverseButton = () => screen.getByRole("button", { name: /^Reverse these points/ });

describe("PointsReversal — the confirmation screen", () => {
  it("shows what is being reversed and what will — and will not — happen", () => {
    open();
    expect(screen.getByRole("heading", { name: "Reverse points" })).toBeInTheDocument();
    const summary = screen.getByText("Ada Lovelace").closest("dl") as HTMLElement;
    expect(summary).toHaveTextContent("#41");
    expect(summary).toHaveTextContent("Hall A");
    expect(summary).toHaveTextContent("Try the demo");
    expect(summary).toHaveTextContent("+10");
    expect(screen.getByText(/The original entry stays in the ledger/)).toBeInTheDocument();
    expect(screen.getByText(/a reversal cannot itself be reversed/)).toBeInTheDocument();
    // the rule after a reversal, stated openly
    expect(screen.getByText(/does NOT make the activity available to this attendee again if it can be earned only once/)).toBeInTheDocument();
  });

  it("says the feedback reward is not restored, for a feedback entry", () => {
    open({ ...ENTRY, source: "FEEDBACK", activity: "Feedback reward" });
    expect(screen.getByText(/does NOT let this attendee earn the feedback reward at this booth again/)).toBeInTheDocument();
    expect(screen.queryByText(/can be earned only once/)).not.toBeInTheDocument();
  });

  it("needs a reason before the button works, and sends nothing for a too-short one", () => {
    const calls = server(() => jsonResponse(200, REVERSED));
    open();
    expect(reverseButton()).toBeDisabled();
    typeReason("ab");
    expect(reverseButton()).toBeEnabled();
    fireEvent.click(reverseButton());
    expect(screen.getByRole("alert")).toHaveTextContent("Give a reason of at least 3 characters.");
    expect(calls).toHaveLength(0);
    typeReason("   \n\t ");
    expect(reverseButton()).toBeDisabled();
  });

  it("counts the characters of the tidied reason and refuses an over-long one", () => {
    const calls = server(() => jsonResponse(200, REVERSED));
    open();
    typeReason("  Wrong   attendee ");
    expect(screen.getByText(/^14\/300/)).toBeInTheDocument(); // "Wrong attendee": counted after tidying
    typeReason("x".repeat(301));
    fireEvent.click(reverseButton());
    expect(screen.getByRole("alert")).toHaveTextContent("The reason can be at most 300 characters.");
    expect(calls).toHaveLength(0);
  });

  it("Cancel leaves without asking the server", () => {
    const calls = server(() => jsonResponse(200, REVERSED));
    const { onClose } = open();
    typeReason("Scanned the wrong person");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledWith(false);
    expect(calls).toHaveLength(0);
  });

  it("shows markup in names as text", () => {
    const { container } = open({ ...ENTRY, attendee_name: "<img src=x onerror=alert(1)>", activity: "<script>x()</script>" });
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});

describe("PointsReversal — the request", () => {
  it("posts only the tidied reason and an idempotency key to the entry's own URL, and claims nothing until the answer", async () => {
    let finish!: (r: Response) => void;
    const calls = server(() => new Promise<Response>((resolve) => { finish = resolve; }));
    const { onClose } = open();
    typeReason("  Wrong   attendee\nscanned ");

    fireEvent.click(reverseButton());

    expect(await screen.findByRole("button", { name: "Reversing…" })).toBeDisabled();
    expect(screen.queryByText("Points reversed")).not.toBeInTheDocument(); // not before the server says so
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("/api/admin/points-log/41/reverse");
    expect(Object.keys(calls[0]!.body).sort()).toEqual(["idempotency_key", "reason"]); // no amount, no attendee
    expect(calls[0]!.body.reason).toBe("Wrong attendee scanned");
    expect(calls[0]!.body.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);

    await act(async () => { finish(jsonResponse(200, REVERSED)); });

    expect(await screen.findByText("Points reversed")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("10 were taken away. The attendee's total is now 25.");
    expect(screen.getByRole("status")).toHaveTextContent("#99");
    fireEvent.click(screen.getByRole("button", { name: "Back to the ledger" }));
    expect(onClose).toHaveBeenCalledWith(true);
  });

  it("a double click is one request", async () => {
    let finish!: (r: Response) => void;
    const calls = server(() => new Promise<Response>((resolve) => { finish = resolve; }));
    open();
    typeReason("Duplicate award");
    const button = reverseButton();
    await act(async () => { button.click(); button.click(); });
    expect(calls).toHaveLength(1);
    await act(async () => { finish(jsonResponse(200, REVERSED)); });
    await screen.findByText("Points reversed");
  });

  it("says so when the server replayed an earlier reversal", async () => {
    server(() => jsonResponse(200, { ...REVERSED, replayed: true }));
    open();
    typeReason("Duplicate award");
    fireEvent.click(reverseButton());
    expect(await screen.findByText(/had already been recorded; nothing was changed a second time/)).toBeInTheDocument();
  });
});

describe("PointsReversal — retries and refusals", () => {
  it("after a lost answer, 'Try again' repeats the SAME request key; a new reason makes a new key", async () => {
    const calls = server((_c, n) => (n === 1 ? Promise.reject(new TypeError("offline")) : n === 2 ? jsonResponse(200, { ...REVERSED, replayed: true }) : jsonResponse(200, REVERSED)));
    open();
    typeReason("The wrong attendee was scanned");
    fireEvent.click(reverseButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("couldn't reach the server");
    expect(screen.getByRole("alert")).toHaveTextContent("Trying again is safe");
    expect(screen.queryByText("Points reversed")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Points reversed")).toBeInTheDocument();
    expect(calls).toHaveLength(2);
    expect(calls[1]!.body.idempotency_key).toBe(calls[0]!.body.idempotency_key);
    expect(calls[1]!.body.reason).toBe(calls[0]!.body.reason);
  });

  it("changing the reason after a failure uses a new key (the server ties a key to its reason)", async () => {
    const calls = server((_c, n) => (n === 1 ? Promise.reject(new TypeError("offline")) : jsonResponse(200, REVERSED)));
    open();
    typeReason("First wording of the reason");
    fireEvent.click(reverseButton());
    await screen.findByRole("alert");
    typeReason("Second wording of the reason");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Points reversed");
    expect(calls[1]!.body.idempotency_key).not.toBe(calls[0]!.body.idempotency_key);
  });

  it.each([
    ["POINTS_ALREADY_REVERSED", 409, /already been reversed/],
    ["POINTS_NOT_REVERSIBLE", 409, /cannot itself be reversed/],
    ["POINTS_ENTRY_NOT_FOUND", 404, /no longer exists/],
  ])("%s: says so and only offers the way back to the ledger", async (code, status, message) => {
    server(() => errorResponse(status, code));
    const { onClose } = open();
    typeReason("Duplicate award");
    fireEvent.click(reverseButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByLabelText(/Reason \(required/)).not.toBeInTheDocument(); // nothing left to submit
    expect(screen.queryByRole("button", { name: /^Reverse these points/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to the ledger" }));
    expect(onClose).toHaveBeenCalledWith(true); // the list must be re-read
  });

  it.each([
    ["UNAUTHENTICATED", 401, /session has ended/],
    ["STAFF_NOT_AUTHORIZED", 403, /cannot access that area/],
    ["MAINTENANCE", 503, /briefly unavailable/],
    ["RATE_LIMITED", 429, /Too many attempts/],
    ["VALIDATION_ERROR", 422, /need attention/],
    ["IDEMPOTENCY_KEY_REUSE", 409, /already been used/],
  ])("%s: shows the matching message and keeps the form for another try", async (code, status, message) => {
    server(() => errorResponse(status, code));
    const { onClose } = open();
    typeReason("Duplicate award");
    fireEvent.click(reverseButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText(/Reason \(required/)).toHaveValue("Duplicate award");
    expect(reverseButton()).toBeEnabled();
    expect(screen.queryByText("Points reversed")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("an unknown error code gets the generic message, never the server's text", async () => {
    server(() => errorResponse(500, "SOMETHING_NEW"));
    open();
    typeReason("Duplicate award");
    fireEvent.click(reverseButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(alert).not.toHaveTextContent("server text");
    await waitFor(() => expect(reverseButton()).toBeEnabled());
  });
});
