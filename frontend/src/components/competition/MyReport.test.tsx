import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MyReport } from "./MyReport";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

const item = (over: object) => ({
  kind: "masterclass", id: 1, title: "AI products", place: null,
  starts_at: "2026-11-13T07:30:00Z", ends_at: "2026-11-13T09:00:00Z", status: "attended", conflict: false, ...over,
});

const report = (over: object) => ({
  eligible: true,
  total: 12,
  categories: [{ category: "MASTERCLASS", points: 12, maximum: 24 }],
  zones: {},
  founder: false,
  lucky_draw: false,
  final: null,
  final_status: "open",
  timezone: "Asia/Baku",
  attended: [],
  not_completed: [],
  panels: [],
  connections: [],
  photos: [],
  ...over,
});

describe("MyReport", () => {
  it("keeps what was completed apart from what was only begun", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, report({
      attended: [item({})],
      not_completed: [
        item({ id: 2, title: "Agents in practice", starts_at: "2026-11-13T12:00:00Z", status: "entered" }),
        item({ kind: "playground", id: 5, title: "AIWorks", starts_at: "2026-11-13T10:00:00Z", status: "checked_in" }),
      ],
    }))));
    render(<MyReport />);
    const done = within(await screen.findByRole("list", { name: "What I completed" }));
    expect(done.getByText(/AI products/)).toBeInTheDocument();
    expect(done.getByText("Completed")).toBeInTheDocument();
    expect(done.queryByText(/Agents in practice/)).toBeNull();

    const begun = within(screen.getByRole("list", { name: "Started, not completed" }));
    expect(begun.getByText(/Agents in practice/)).toBeInTheDocument();
    expect(begun.getByText("Entry scanned, exit not scanned: not completed, no points.")).toBeInTheDocument();
    expect(begun.getByText("Admitted, but the end of the session was not scanned: not completed, no points.")).toBeInTheDocument();
    expect(begun.queryByText("Completed")).toBeNull();
  });

  it("shows no begun-only section when there is nothing of the kind, or an older server sends none", async () => {
    const { not_completed: _omitted, ...older } = report({ attended: [item({})] });
    void _omitted;
    vi.stubGlobal("fetch", vi.fn(async () => json(200, older)));
    render(<MyReport />);
    await screen.findByRole("list", { name: "What I completed" });
    expect(screen.queryByText("Started, not completed")).toBeNull();
  });
});
