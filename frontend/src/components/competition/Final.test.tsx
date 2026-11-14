import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FinalReview } from "./FinalReview";
import { FinalScreen } from "./FinalScreen";
import { PanelScan } from "./PanelScan";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("FinalScreen", () => {
  it("says the result is under review until a Game Master publishes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { status: "pending_review", closes_at: "2026-11-13T13:30:00Z" })));
    render(<FinalScreen />);
    expect(await screen.findByRole("status")).toHaveTextContent("The Game Master is reviewing the results.");
    expect(screen.queryByText(/Final ranking/)).toBeNull();
  });

  it("shows only the published version: places, short names and the Founders", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, {
      status: "published", closes_at: null, version: 2, published_at: "2026-11-13T13:39:00Z",
      top: [{ name: "Aysel M.", photo_url: null, place: 1, points: 38 }, { name: "Orxan Q.", photo_url: null, place: 2, points: 30 }],
      founders: [{ name: "Aysel M.", photo_url: null }],
    })));
    const { container } = render(<FinalScreen />);
    const first = within(await screen.findByTestId("final-1"));
    expect(first.getByText("Aysel M.")).toBeInTheDocument();
    expect(first.getByText("38")).toBeInTheDocument();
    expect(screen.getByTestId("final-2")).toHaveTextContent("Orxan Q.");
    expect(screen.getByText("Result version 2")).toBeInTheDocument();
    expect(container.querySelectorAll(".fm-chip")).toHaveLength(1);
    expect(container.textContent).not.toContain("@");
  });
});

describe("FinalReview", () => {
  it("publishes a draft only after confirmation and shows when the ledger moved on", async () => {
    const posts: string[] = [];
    const detail = { id: 7, version: 1, status: "draft", created_at: "", published_at: null, entries: 2, founders: 1,
      ranking: [{ place: 1, user_id: 3, name: "Aysel M.", points: 38, reached_at: null }], founder_list: [], ledger_changed_since: true };
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") { posts.push(url); return json(200, { ...detail, status: "published" }); }
      if (url === "/api/staff/final") return json(200, { closes_at: null, timezone: "Asia/Baku", pending_offline_scans: 0, versions: [detail] });
      if (url === "/api/staff/offline-scans") return json(200, { scans: [] });
      return json(200, detail);
    }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<FinalReview />);
    const card = within(await screen.findByTestId("version-1"));
    fireEvent.click(card.getByRole("button", { name: "Publish" }));
    expect(posts).toEqual([]);
    fireEvent.click(card.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(posts).toEqual(["/api/staff/final/7/publish"]));
    expect(await screen.findByText(/Points changed after this snapshot/)).toBeInTheDocument();
    confirm.mockRestore();
  });
});

describe("PanelScan — Founder", () => {
  it("shows the four zones and the Lucky Draw place from the server", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/panels") return json(200, []);
      return json(200, {
        eligible: true, total: 38, maximum: 100, categories: [],
        zones: { main_stage: true, masterclass: true, playground: true, connection_hub: false },
        founder: false, lucky_draw: false, final: null, final_status: "pending_review",
      });
    }));
    const { container } = render(<PanelScan />);
    const founder = within(await screen.findByRole("region", { name: "Beyond Now Founder" }));
    expect(founder.getByText(/Connection Hub/).closest("li")).toHaveAttribute("data-done", "false");
    expect(container.querySelectorAll("[data-done='true']")).toHaveLength(3);
    expect(screen.getByText("The ranking has closed. The Game Master is reviewing the results.")).toBeInTheDocument();
    expect(founder.queryByText(/You are a Beyond Now Founder/)).toBeNull();
  });
});
