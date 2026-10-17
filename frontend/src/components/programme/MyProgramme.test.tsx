import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MyProgramme } from "./MyProgramme";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

const item = (over: object) => ({
  kind: "masterclass", id: 1, title: "AI products", place: "Hall 1",
  starts_at: "2026-11-13T07:30:00Z", ends_at: "2026-11-13T09:00:00Z", status: "confirmed", conflict: false, ...over,
});

describe("MyProgramme", () => {
  it("lists the server's items in event time with their real status", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, {
      timezone: "Asia/Baku",
      items: [
        item({}),
        item({ kind: "meeting", id: 4, title: "Aysel M.", place: "M2", starts_at: "2026-11-13T09:15:00Z", ends_at: "2026-11-13T09:30:00Z", status: "scheduled" }),
        item({ kind: "mentor", id: 9, title: "Careers in AI", place: null, starts_at: "2026-11-13T09:35:00Z", ends_at: "2026-11-13T09:55:00Z", status: "waitlisted" }),
      ],
    })));
    render(<MyProgramme />);
    const mc = within(await screen.findByTestId("prog-masterclass-1"));
    expect(mc.getByText("11:30–13:00")).toBeInTheDocument();
    expect(mc.getByText("Confirmed")).toBeInTheDocument();
    expect(within(screen.getByTestId("prog-meeting-4")).getByText("Table M2")).toBeInTheDocument();
    expect(within(screen.getByTestId("prog-mentor-9")).getByText("On the waiting list")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("warns about a clash the server reports", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { timezone: "Asia/Baku", items: [item({ conflict: true })] })));
    render(<MyProgramme />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This overlaps another item of your programme.");
  });

  it("asks a signed-out visitor to sign in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(401, { error: { code: "UNAUTHENTICATED", message: "", details: {} } })));
    render(<MyProgramme />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });
});
