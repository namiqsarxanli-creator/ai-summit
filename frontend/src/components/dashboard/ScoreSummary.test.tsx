import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";
import { installMockEventSource, MockEventSource } from "@/test-utils/mockEventSource";
import { teardownRealtime } from "@/test-utils/realtimeTeardown";

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

// M14 wires ScoreSummary to the shared realtime owner (`lib/realtime.ts`)
// instead of its own one-shot fetch: mounting it opens an EventSource,
// and the score snapshot is only fetched once `ready` arrives (§17.4's
// subscribe-first-then-snapshot ordering) — these tests drive that
// mock connection explicitly rather than stubbing `fetchMyScore`
// directly, so they exercise the same path the real component takes.
function openReady(): void {
  MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
}

beforeEach(() => {
  installMockEventSource();
});

afterEach(() => {
  teardownRealtime();
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
    openReady();
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
    openReady();
    expect(await screen.findByText("0")).toBeInTheDocument();
    expect(screen.getByText("Rank #12")).toBeInTheDocument();
  });

  it("renders nothing on the signed-out race (session expired between fetches)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    const { container } = render(<ScoreSummary />);
    openReady();
    await vi.waitFor(() => expect(container.textContent).not.toContain("Loading"));
    expect(container.querySelector('[role="alert"]')).not.toBeInTheDocument();
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<ScoreSummary />);
    openReady();
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("shows a loading state before `ready` arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<ScoreSummary />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows a transient award banner when a new recent award arrives after mount", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        if (call === 1) {
          return jsonResponse(200, {
            total_points: 10,
            rank: 5,
            recent_awards: [],
            as_of: "2026-09-22T17:04:11Z",
            dataset_generation: 1,
          });
        }
        return jsonResponse(200, {
          total_points: 15,
          rank: 4,
          recent_awards: [
            {
              id: 9,
              points: 5,
              booth_name: "AIWorks",
              source: "FEEDBACK",
              created_at: new Date().toISOString(),
            },
          ],
          as_of: "2026-09-22T17:05:00Z",
          dataset_generation: 1,
        });
      }),
    );
    render(<ScoreSummary />);
    openReady();
    await screen.findByText("10");

    MockEventSource.latest().emit("state_changed", { scope: "personal" });

    expect(await screen.findByText("+5 points!")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
  });
});
