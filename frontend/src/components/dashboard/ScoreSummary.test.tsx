import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { ScoreSummary } from "./ScoreSummary";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ScoreSummary", () => {
  it("shows the server-computed total and rank exactly as returned", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          total_points: 45,
          rank: 3,
          recent_awards: [],
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 7,
        }),
      ),
    );
    render(<ScoreSummary />);
    expect(await screen.findByText("45")).toBeInTheDocument();
    expect(screen.getByText("Rank #3")).toBeInTheDocument();
  });

  it("shows a zero total honestly, not blank", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          total_points: 0,
          rank: 12,
          recent_awards: [],
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 7,
        }),
      ),
    );
    render(<ScoreSummary />);
    expect(await screen.findByText("0")).toBeInTheDocument();
    expect(screen.getByText("Rank #12")).toBeInTheDocument();
  });

  it("renders nothing on the signed-out race (session expired between fetches)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    const { container } = render(<ScoreSummary />);
    await vi.waitFor(() => expect(container.textContent).not.toContain("Loading"));
    expect(container.querySelector('[role="alert"]')).not.toBeInTheDocument();
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<ScoreSummary />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("shows a loading state before data arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<ScoreSummary />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });
});
