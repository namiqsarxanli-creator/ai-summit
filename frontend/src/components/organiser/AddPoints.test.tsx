import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddPoints } from "./AddPoints";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function fill() {
  fireEvent.change(screen.getByLabelText("Attendee ID"), { target: { value: "18" } });
  fireEvent.change(screen.getByLabelText("Points"), { target: { value: "12" } });
  fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "The scan was missed." } });
}

describe("AddPoints", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("needs a reason, confirms, and reports who got the points", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const fetchMock = vi.fn(async () => json(200, { name: "Turan Səmədli", outcome: "awarded", points: 12 }));
    vi.stubGlobal("fetch", fetchMock);
    const onAdded = vi.fn();
    render(<AddPoints onAdded={onAdded} />);
    const button = screen.getByRole("button", { name: "Add points", hidden: true });
    expect(button).toBeDisabled();
    fill();
    fireEvent.click(button);
    expect(await screen.findByRole("status", { hidden: true })).toHaveTextContent("Turan Səmədli · +12");
    expect(onAdded).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/points-log/award");
    expect(JSON.parse(String(init.body))).toMatchObject({ user_id: 18, source: "MASTERCLASS", points: 12, reason: "The scan was missed." });
  });

  it("says so when a category is already full, and adds nothing to the list", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { name: "Turan Səmədli", outcome: "category_full", points: 0 })));
    const onAdded = vi.fn();
    render(<AddPoints onAdded={onAdded} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Add points", hidden: true }));
    await waitFor(() => expect(screen.getByRole("alert", { hidden: true })).toHaveTextContent("already at its maximum"));
    expect(onAdded).not.toHaveBeenCalled();
  });
});
