import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UnpaidConnections } from "./UnpaidConnections";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

const ITEMS = [
  { connection_id: 4, user_id: 11, name: "Anar N.", with: "Aysel M.", kind: "match", points: 3, established_at: "2026-11-13T08:00:00Z", category: "eligible" },
  { connection_id: 5, user_id: 12, name: "Leyla H.", with: "Murad Q.", kind: "other", points: 1, established_at: "2026-11-12T08:00:00Z", category: "needs_decision" },
];

function server(review: () => Response, apply?: () => Response) {
  const writes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { writes.push(JSON.parse(String(init.body))); return apply ? apply() : json(200, { results: [] }); }
    return review();
  }));
  return writes;
}

describe("UnpaidConnections", () => {
  it("says so when there is nothing unpaid", async () => {
    server(() => json(200, { items: [], counts: {}, applicable: "eligible" }));
    render(<UnpaidConnections />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(await screen.findByText("Every connection holds the points it earned.")).toBeInTheDocument();
  });

  it("shows the server's error instead of a list", async () => {
    server(() => json(403, { error: { code: "ADMIN_REQUIRED", message: "", details: {}, correlation_id: "c" } }));
    render(<UnpaidConnections />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("lets only eligible sides be chosen and needs a reason and a confirmation", async () => {
    const writes = server(
      () => json(200, { items: ITEMS, counts: { eligible: 1, needs_decision: 1 }, applicable: "eligible" }),
      () => json(200, { results: [{ connection_id: 4, user_id: 11, result: "awarded", points: 3 }] }),
    );
    render(<UnpaidConnections />);
    const list = within(await screen.findByRole("list", { name: "Connections without points" }));
    const [eligible, undecided] = list.getAllByRole("checkbox");
    expect(undecided).toBeDisabled(); // an organiser decision, not a payment
    const pay = () => screen.getByRole("button", { name: /Pay the selected connections/ });
    expect(pay()).toBeDisabled();
    fireEvent.click(eligible!);
    expect(pay()).toBeDisabled(); // no reason, no confirmation
    fireEvent.change(screen.getByLabelText("Reason (required, 3–300 characters)"), { target: { value: "Close was set late" } });
    expect(pay()).toBeDisabled();
    fireEvent.click(screen.getByLabelText("I checked these connections and confirm the payment through the normal rules."));
    fireEvent.click(pay());
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toEqual({ items: [{ connection_id: 4, user_id: 11 }], reason: "Close was set late", confirm: true });
    expect(await screen.findByRole("status", { name: "Result" })).toHaveTextContent("#4 · Paid. · +3");
  });
});
