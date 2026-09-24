import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchMyScore } from "./score";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchMyScore", () => {
  it("GETs the authoritative snapshot from the fixed same-origin path", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        total_points: 45,
        rank: 3,
        recent_awards: [
          {
            id: 481,
            points: 15,
            booth_name: "AIWorks",
            source: "TASK",
            created_at: "2026-09-22T17:04:11Z",
          },
        ],
        as_of: "2026-09-22T17:04:12Z",
        dataset_generation: 7,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchMyScore();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/score");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.total_points).toBe(45);
      expect(result.data.rank).toBe(3);
      expect(result.data.recent_awards).toHaveLength(1);
      expect(result.data.dataset_generation).toBe(7);
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
    const result = await fetchMyScore();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("UNAUTHENTICATED");
  });
});
