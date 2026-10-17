import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";
import { installMockEventSource, MockEventSource } from "@/test-utils/mockEventSource";
import { teardownRealtime } from "@/test-utils/realtimeTeardown";

import { LeaderboardView } from "./LeaderboardView";

// M14 wires the default top-N view to the shared realtime owner
// (`lib/realtime.ts`): mounting opens an EventSource, and the
// leaderboard snapshot is only fetched once `ready` arrives — these
// tests drive that mock connection explicitly. "Show all" pagination
// is untouched local component state (still its own direct fetch).
function openReady(): void {
  MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

function entry(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    user_id: 1,
    first_name: "Ada",
    last_name: "Lovelace",
    profession: "Engineer",
    total_points: 45,
    rank: 3,
    ...overrides,
  };
}

beforeEach(() => {
  installMockEventSource();
});

afterEach(() => {
  teardownRealtime();
});

describe("LeaderboardView — top view", () => {
  it("shows a loading state before `ready` arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<LeaderboardView />);
    expect(screen.getByText("Loading leaderboard…")).toBeInTheDocument();
  });

  it("offers a sign-in link when signed out", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    render(<LeaderboardView />);
    openReady();
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });

  it("a signed-out visitor sees the sign-in link even though the stream refuses them (no ready ever comes)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    render(<LeaderboardView />);
    act(() => MockEventSource.latest().refuse()); // the live site answers the stream with 401
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.queryByText("Loading leaderboard…")).not.toBeInTheDocument();
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<LeaderboardView />);
    openReady();
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("shows a neutral message when nobody has joined yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          entries: [],
          current_user: null,
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        }),
      ),
    );
    render(<LeaderboardView />);
    openReady();
    expect(await screen.findByText(/No attendees have joined the leaderboard yet/)).toBeInTheDocument();
  });

  it("renders ranked entries with name, profession and points", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          entries: [
            entry({ user_id: 1, first_name: "Ada", last_name: "Lovelace", rank: 1, total_points: 90 }),
            entry({ user_id: 2, first_name: "Grace", last_name: "Hopper", rank: 2, total_points: 80 }),
          ],
          current_user: null,
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        }),
      ),
    );
    render(<LeaderboardView />);
    openReady();
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getByText("90 pts")).toBeInTheDocument();
    expect(screen.getByText("#1")).toBeInTheDocument();
  });

  it("shows the caller's own position separately when outside the top N", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          entries: [entry({ user_id: 1, first_name: "Ada", last_name: "Lovelace", rank: 1 })],
          current_user: entry({ user_id: 99, first_name: "Outside", last_name: "Attendee", rank: 42, total_points: 2 }),
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        }),
      ),
    );
    render(<LeaderboardView />);
    openReady();
    expect(await screen.findByText("Your position")).toBeInTheDocument();
    expect(screen.getByText("Outside Attendee")).toBeInTheDocument();
    expect(screen.getByText("#42")).toBeInTheDocument();
  });

  it("does not duplicate the caller's row when they are already inside the top N", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          entries: [entry({ user_id: 1, first_name: "Ada", last_name: "Lovelace", rank: 1 })],
          current_user: entry({ user_id: 1, first_name: "Ada", last_name: "Lovelace", rank: 1 }),
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        }),
      ),
    );
    render(<LeaderboardView />);
    openReady();
    await screen.findByText("Ada Lovelace");
    expect(screen.queryByText("Your position")).not.toBeInTheDocument();
    expect(screen.getAllByText("Ada Lovelace")).toHaveLength(1);
  });

  it("shows the pushed leaderboard when another attendee's score changes it (broadcast)", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        return jsonResponse(200, {
          entries: [
            entry({
              user_id: 1,
              first_name: call === 1 ? "Ada" : "Ada-Updated",
              last_name: "Lovelace",
              rank: call === 1 ? 2 : 1,
              total_points: call === 1 ? 50 : 90,
            }),
          ],
          current_user: null,
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        });
      }),
    );
    render(<LeaderboardView />);
    openReady();
    await screen.findByText("#2");

    MockEventSource.latest().emit("leaderboard_changed", {
      leaderboard: {
        entries: [
          entry({ user_id: 1, first_name: "Ada-Updated", last_name: "Lovelace", rank: 1, total_points: 90 }),
        ],
        dataset_generation: 1,
        version: 1,
      },
    });

    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(call).toBe(1); // applied from the push, no refetch
    expect(screen.getByText("90 pts")).toBeInTheDocument();
  });
});

describe("LeaderboardView — show all", () => {
  function topResponse() {
    return jsonResponse(200, {
      entries: [entry({ user_id: 1, rank: 1 })],
      current_user: null,
      as_of: "2026-09-22T17:04:11Z",
      dataset_generation: 5,
      next_cursor: null,
    });
  }

  it("loads the full list on demand, showing as_of", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/leaderboard") return topResponse();
      return jsonResponse(200, {
        entries: [entry({ user_id: 2, first_name: "Grace", last_name: "Hopper", rank: 2 })],
        current_user: null,
        as_of: "2026-09-22T18:00:00Z",
        dataset_generation: 5,
        next_cursor: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<LeaderboardView />);
    openReady();
    await screen.findByRole("button", { name: "Show all" });

    await act(async () => {
      screen.getByRole("button", { name: "Show all" }).click();
    });

    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getByText(/As of/)).toBeInTheDocument();
  });

  it("loads more pages and stops offering the button once next_cursor is null", async () => {
    let call = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/leaderboard") return topResponse();
      call += 1;
      if (call === 1) {
        return jsonResponse(200, {
          entries: [entry({ user_id: 2, first_name: "Page", last_name: "One", rank: 2 })],
          current_user: null,
          as_of: "2026-09-22T18:00:00Z",
          dataset_generation: 5,
          next_cursor: "cursor-1",
        });
      }
      return jsonResponse(200, {
        entries: [entry({ user_id: 3, first_name: "Page", last_name: "Two", rank: 3 })],
        current_user: null,
        as_of: "2026-09-22T18:01:00Z",
        dataset_generation: 5,
        next_cursor: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<LeaderboardView />);
    openReady();
    await screen.findByRole("button", { name: "Show all" });
    await act(async () => {
      screen.getByRole("button", { name: "Show all" }).click();
    });
    await screen.findByText("Page One");
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: "Load more" }).click();
    });
    await screen.findByText("Page Two");
    // Both pages' rows remain — accumulated, not replaced.
    expect(screen.getByText("Page One")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    // The cursor from page 1 was passed through verbatim to fetch page 2.
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/leaderboard?view=all&cursor=cursor-1")).toBe(
      true,
    );
  });

  it("discards accumulated pages and restarts when dataset_generation changes mid-pagination", async () => {
    let call = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/leaderboard") return topResponse();
      call += 1;
      if (call === 1) {
        return jsonResponse(200, {
          entries: [entry({ user_id: 2, first_name: "Stale", last_name: "Page", rank: 2 })],
          current_user: null,
          as_of: "2026-09-22T18:00:00Z",
          dataset_generation: 5,
          next_cursor: "cursor-1",
        });
      }
      if (call === 2) {
        // A reset happened: generation jumped from 5 to 6.
        return jsonResponse(200, {
          entries: [entry({ user_id: 9, first_name: "Post", last_name: "Reset", rank: 1 })],
          current_user: null,
          as_of: "2026-09-22T19:00:00Z",
          dataset_generation: 6,
          next_cursor: null,
        });
      }
      return jsonResponse(200, {
        entries: [entry({ user_id: 9, first_name: "Post", last_name: "Reset", rank: 1 })],
        current_user: null,
        as_of: "2026-09-22T19:00:00Z",
        dataset_generation: 6,
        next_cursor: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<LeaderboardView />);
    openReady();
    await screen.findByRole("button", { name: "Show all" });
    await act(async () => {
      screen.getByRole("button", { name: "Show all" }).click();
    });
    await screen.findByText("Stale Page");

    await act(async () => {
      screen.getByRole("button", { name: "Load more" }).click();
    });

    // The stale-generation page's rows are discarded, not appended —
    // the listing restarts from a fresh first page instead.
    expect(await screen.findByText("Post Reset")).toBeInTheDocument();
    expect(screen.queryByText("Stale Page")).not.toBeInTheDocument();
  });
});

describe("LeaderboardView — avatars", () => {
  const KEY = "0123456789abcdef0123456789abcdef";
  const SMALL = `/api/media/avatars/${KEY}-128.jpg`;

  function board(entries: unknown[]) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          entries,
          current_user: null,
          as_of: "2026-09-22T17:04:11Z",
          dataset_generation: 1,
          next_cursor: null,
        }),
      ),
    );
  }

  it("shows an attendee's photo, and the robot for everyone without one", async () => {
    board([
      entry({ user_id: 1, first_name: "Ada", last_name: "Lovelace", rank: 1, photo_url: SMALL }),
      entry({ user_id: 2, first_name: "Grace", last_name: "Hopper", rank: 2, photo_url: null }),
      entry({ user_id: 3, first_name: "Alan", last_name: "Turing", rank: 3 }), // an older server: no field
    ]);
    const { container } = render(<LeaderboardView />);
    openReady();
    await screen.findByText("Ada Lovelace");
    const rows = Array.from(container.querySelectorAll("li.fm-rank-row"));
    expect(rows).toHaveLength(3);
    const photos = rows.map((row) => row.querySelector(".fm-rank-avatar img")?.getAttribute("src") ?? null);
    expect(photos[0]).toContain(KEY);
    expect(photos[1]).toBeNull();
    expect(photos[2]).toBeNull();
    // every row still has its circle, so the robot shows where there is no photo
    for (const row of rows) expect(row.querySelector(".fm-rank-avatar")).not.toBeNull();
  });

  it("falls back to the robot when a photo does not load", async () => {
    board([entry({ rank: 1, photo_url: SMALL })]);
    const { container } = render(<LeaderboardView />);
    openReady();
    await screen.findByText("Ada Lovelace");
    const image = container.querySelector(".fm-rank-avatar img") as HTMLImageElement;
    fireEvent.error(image);
    expect(container.querySelector(".fm-rank-avatar img")).toBeNull();
    expect(container.querySelector(".fm-rank-avatar")).toHaveAttribute("data-photo", "false");
  });

  it("never loads a photo URL that is not one the server makes", async () => {
    board([
      entry({ user_id: 1, rank: 1, photo_url: "https://tracker.example/pixel.gif" }),
      entry({ user_id: 2, first_name: "B", rank: 2, photo_url: "javascript:alert(1)" }),
    ]);
    const { container } = render(<LeaderboardView />);
    openReady();
    await screen.findByText("Ada Lovelace");
    expect(container.querySelector("img")).toBeNull();
  });

  it("does not put anything private next to the photo", async () => {
    board([entry({ rank: 1, photo_url: SMALL })]);
    const { container } = render(<LeaderboardView />);
    openReady();
    await screen.findByText("Ada Lovelace");
    expect(container.textContent).not.toMatch(/@|\+\d|consent/i);
  });
});
