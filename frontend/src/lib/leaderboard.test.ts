import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchLeaderboardPage, fetchLeaderboardTop } from "./leaderboard";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ENTRY = {
  user_id: 12,
  first_name: "Ada",
  last_name: "Lovelace",
  profession: "Engineer",
  total_points: 45,
  rank: 3,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchLeaderboardTop", () => {
  it("GETs the default view with no query parameters", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        entries: [ENTRY],
        current_user: ENTRY,
        as_of: "2026-09-22T17:04:11Z",
        dataset_generation: 7,
        next_cursor: null,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchLeaderboardTop();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/leaderboard");
    expect(init.credentials).toBe("same-origin");

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.entries).toEqual([ENTRY]);
      expect(result.data.current_user).toEqual(ENTRY);
      expect(result.data.next_cursor).toBeNull();
    }
  });

  it("surfaces UNAUTHENTICATED for a signed-out visitor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(401, {
          error: { code: "UNAUTHENTICATED", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await fetchLeaderboardTop();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("UNAUTHENTICATED");
  });
});

describe("fetchLeaderboardPage", () => {
  it("requests the show-all view with no cursor on the first page", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        entries: [ENTRY],
        current_user: null,
        as_of: "2026-09-22T17:04:11Z",
        dataset_generation: 7,
        next_cursor: "opaque-cursor-value",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchLeaderboardPage(null);

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/leaderboard?view=all");
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.next_cursor).toBe("opaque-cursor-value");
  });

  it("round-trips a server-issued cursor verbatim, never constructing one itself", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        entries: [],
        current_user: null,
        as_of: "2026-09-22T17:04:11Z",
        dataset_generation: 7,
        next_cursor: null,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchLeaderboardPage("abc123.opaque");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("/api/leaderboard?view=all&cursor=abc123.opaque");
  });
});
