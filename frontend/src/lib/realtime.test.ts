import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installMockEventSource, MockEventSource } from "@/test-utils/mockEventSource";

import { realtimeStore } from "./realtime";

// Flushes real macrotasks/microtasks — a fetch mock's `Response.json()`
// resolves via the underlying stream implementation, which needs more
// than a couple of bare `Promise.resolve()` ticks to fully settle.
// Not used inside fake-timer tests (real timers only).
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
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

const SCORE_BODY = {
  total_points: 10,
  rank: 2,
  recent_awards: [],
  as_of: "2026-09-24T00:00:00Z",
  dataset_generation: 1,
};

const LEADERBOARD_BODY = {
  entries: [],
  current_user: null,
  as_of: "2026-09-24T00:00:00Z",
  dataset_generation: 1,
  next_cursor: null,
};

function fetchRouter(overrides: Record<string, () => Response> = {}) {
  return vi.fn(async (url: string) => {
    const override = overrides[url];
    if (override) return override();
    if (url === "/api/attendees/me/score") return jsonResponse(200, SCORE_BODY);
    if (url === "/api/leaderboard") return jsonResponse(200, LEADERBOARD_BODY);
    throw new Error(`unexpected fetch to ${url}`);
  });
}

beforeEach(() => {
  installMockEventSource();
});

afterEach(() => {
  realtimeStore.resetForTests();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("subscribe-first-then-snapshot", () => {
  it("opens the EventSource but fetches nothing before `ready`", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    await flush();

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.latest().url).toBe("/api/realtime/stream");
    expect(fetchMock).not.toHaveBeenCalled();

    unsubscribe();
  });

  it("fetches the score snapshot only after `ready` arrives", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    expect(fetchMock).toHaveBeenCalledWith("/api/attendees/me/score", expect.anything());
    expect(realtimeStore.getScoreSnapshot()).toEqual({
      status: "ok",
      score: SCORE_BODY,
      justAwardedPoints: null,
    });

    unsubscribe();
  });
});

describe("signal-driven refetch", () => {
  it("state_changed refetches only the score, not the leaderboard", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    fetchMock.mockClear();

    MockEventSource.latest().emit("state_changed", { scope: "personal" });
    await flush();
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/attendees/me/score", expect.anything());
    unsubscribe();
  });

  it("leaderboard_changed refetches the leaderboard only while it is mounted", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribeScore = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    fetchMock.mockClear();

    MockEventSource.latest().emit("leaderboard_changed", {});
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();

    const unsubscribeLeaderboard = realtimeStore.subscribeLeaderboard(() => {});
    await flush();
    await flush();
    fetchMock.mockClear();

    MockEventSource.latest().emit("leaderboard_changed", {});
    await flush();
    await flush();
    expect(fetchMock).toHaveBeenCalledWith("/api/leaderboard", expect.anything());

    unsubscribeScore();
    unsubscribeLeaderboard();
  });
});

describe("periodic reconciliation (§17.4 step 5)", () => {
  it("refetches every 20s even with no signal at all", async () => {
    vi.useFakeTimers();
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();

    await vi.advanceTimersByTimeAsync(20_000);
    expect(fetchMock).toHaveBeenCalledWith("/api/attendees/me/score", expect.anything());

    unsubscribe();
  });
});

describe("dead-connection detection (§17.9)", () => {
  it("closes and reopens the stream after 45s with no frame of any kind", async () => {
    vi.useFakeTimers();
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const first = MockEventSource.latest();
    first.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await vi.advanceTimersByTimeAsync(0);

    await vi.advanceTimersByTimeAsync(45_000);

    expect(first.closed).toBe(true);
    expect(MockEventSource.instances).toHaveLength(2);
    unsubscribe();
  });

  it("a heartbeat resets the 45s dead timer", async () => {
    vi.useFakeTimers();
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const first = MockEventSource.latest();
    first.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await vi.advanceTimersByTimeAsync(0);

    await vi.advanceTimersByTimeAsync(30_000);
    first.emit("heartbeat", { t: "2026-09-24T00:00:00Z" });
    await vi.advanceTimersByTimeAsync(30_000); // 60s total, but heartbeat reset the clock at 30s

    expect(first.closed).toBe(false);
    expect(MockEventSource.instances).toHaveLength(1);
    unsubscribe();
  });
});

describe("session_invalid", () => {
  it("closes the stream and signs the surfaces out", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const es = MockEventSource.latest();
    es.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    es.emit("session_invalid", { reason: "revoked" });

    expect(es.closed).toBe(true);
    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "signed-out" });
    unsubscribe();
  });
});

describe("recent-award animation (§17.6)", () => {
  it("the first snapshot seeds animatedAwardIds without animating", async () => {
    const fetchMock = fetchRouter({
      "/api/attendees/me/score": () =>
        jsonResponse(200, {
          ...SCORE_BODY,
          recent_awards: [
            { id: 1, points: 5, booth_name: "AIWorks", source: "FEEDBACK", created_at: "2026-01-01T00:00:00Z" },
          ],
        }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    const state = realtimeStore.getScoreSnapshot();
    expect(state.status).toBe("ok");
    if (state.status === "ok") expect(state.justAwardedPoints).toBeNull();
    unsubscribe();
  });

  it("an unseen award in a later snapshot animates exactly once", async () => {
    let call = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url !== "/api/attendees/me/score") throw new Error(`unexpected fetch to ${url}`);
      call += 1;
      if (call === 1) return jsonResponse(200, { ...SCORE_BODY, recent_awards: [] });
      return jsonResponse(200, {
        ...SCORE_BODY,
        total_points: 15,
        recent_awards: [
          {
            id: 2,
            points: 5,
            booth_name: "AIWorks",
            source: "FEEDBACK",
            created_at: new Date().toISOString(),
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    MockEventSource.latest().emit("state_changed", { scope: "personal" });
    await flush();
    await flush();

    const state = realtimeStore.getScoreSnapshot();
    expect(state.status).toBe("ok");
    if (state.status === "ok") expect(state.justAwardedPoints).toBe(5);

    // A further refetch (e.g. reconciliation) never re-animates the same id.
    MockEventSource.latest().emit("state_changed", { scope: "personal" });
    await flush();
    await flush();
    const state2 = realtimeStore.getScoreSnapshot();
    if (state2.status === "ok") expect(state2.justAwardedPoints).toBeNull();

    unsubscribe();
  });
});

describe("dataset generation mismatch", () => {
  it("hard-reinitialises on a mismatched dataset_reset generation", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const es = MockEventSource.latest();
    es.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    expect(realtimeStore.getScoreSnapshot().status).toBe("ok");

    es.emit("dataset_reset", { dataset_generation: 2 });

    expect(es.closed).toBe(true);
    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "loading" });
    unsubscribe();
  });
});

describe("fallback polling (§17.11)", () => {
  it("falls back to polling after 3 consecutive connection failures", async () => {
    vi.useFakeTimers();
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);

    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const es = MockEventSource.latest();
    es.error();
    es.error();
    es.error();

    await vi.advanceTimersByTimeAsync(0);
    fetchMock.mockClear();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(fetchMock).toHaveBeenCalledWith("/api/attendees/me/score", expect.anything());

    unsubscribe();
  });
});

describe("unsubscribe", () => {
  it("closes the stream once every subscriber has gone", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const es = MockEventSource.latest();
    es.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();

    unsubscribe();
    expect(es.closed).toBe(true);
  });
});

describe("stale-response race protection", () => {
  // A slower, earlier-started request must never overwrite a faster,
  // later-started one — deterministic proof, not timing-dependent:
  // both requests are held open with controllable promises, and B is
  // resolved before A regardless of which one was issued first.
  function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((r) => {
      resolve = r;
    });
    return { promise, resolve };
  }

  it("a newer score snapshot is not overwritten by an older, slower response (request A/B)", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    fetchMock.mockClear();

    const responses = [
      deferred<Response>(), // Request A (issued first)
      deferred<Response>(), // Request B (issued second)
    ];
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url !== "/api/attendees/me/score") throw new Error(`unexpected fetch to ${url}`);
        const index = call;
        call += 1;
        return responses[index]!.promise;
      }),
    );

    // Request A starts...
    MockEventSource.latest().emit("state_changed", { scope: "personal" });
    await flush();
    // ...then, while A is still in flight, Request B starts later.
    MockEventSource.latest().emit("state_changed", { scope: "personal" });
    await flush();
    expect(call).toBe(2);

    // B (the NEWER request) returns FIRST, with newer authoritative state.
    responses[1]!.resolve(jsonResponse(200, { ...SCORE_BODY, total_points: 99 }));
    await flush();
    await flush();
    expect(realtimeStore.getScoreSnapshot()).toMatchObject({
      status: "ok",
      score: { total_points: 99 },
    });

    // A (the OLDER request) returns AFTERWARD, with older/stale state.
    responses[0]!.resolve(jsonResponse(200, { ...SCORE_BODY, total_points: 10 }));
    await flush();
    await flush();

    // B's newer state must survive — A's late, stale arrival must not
    // overwrite it.
    expect(realtimeStore.getScoreSnapshot()).toMatchObject({
      status: "ok",
      score: { total_points: 99 },
    });

    unsubscribe();
  });

  it("a newer leaderboard snapshot is not overwritten by an older, slower response (request A/B)", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeLeaderboard(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    fetchMock.mockClear();

    const responses = [
      deferred<Response>(), // Request A (issued first)
      deferred<Response>(), // Request B (issued second)
    ];
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url !== "/api/leaderboard") throw new Error(`unexpected fetch to ${url}`);
        const index = call;
        call += 1;
        return responses[index]!.promise;
      }),
    );

    MockEventSource.latest().emit("leaderboard_changed", {}); // Request A
    await flush();
    MockEventSource.latest().emit("leaderboard_changed", {}); // Request B, later
    await flush();
    expect(call).toBe(2);

    const newerEntry = { attendee_name: "Newer", total_points: 99, rank: 1, is_current_user: false };
    const olderEntry = { attendee_name: "Older", total_points: 10, rank: 1, is_current_user: false };

    // B (newer) resolves first.
    responses[1]!.resolve(
      jsonResponse(200, { ...LEADERBOARD_BODY, entries: [newerEntry] }),
    );
    await flush();
    await flush();
    expect(realtimeStore.getLeaderboardSnapshot()).toMatchObject({
      status: "ok",
      leaderboard: { entries: [newerEntry] },
    });

    // A (older) resolves after — must not overwrite B's result.
    responses[0]!.resolve(
      jsonResponse(200, { ...LEADERBOARD_BODY, entries: [olderEntry] }),
    );
    await flush();
    await flush();
    expect(realtimeStore.getLeaderboardSnapshot()).toMatchObject({
      status: "ok",
      leaderboard: { entries: [newerEntry] },
    });

    unsubscribe();
  });

  it("a request in flight before a generation-mismatch hard reset cannot restore pre-reset state", async () => {
    const fetchMock = fetchRouter();
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    const es = MockEventSource.latest();
    es.emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();
    expect(realtimeStore.getScoreSnapshot().status).toBe("ok");

    // Request A (old-generation) starts, and is held open.
    const stale = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url !== "/api/attendees/me/score") throw new Error(`unexpected fetch to ${url}`);
        return stale.promise;
      }),
    );
    es.emit("state_changed", { scope: "personal" });
    await flush();

    // A generation mismatch / hard reset occurs while A is still in flight.
    es.emit("dataset_reset", { dataset_generation: 2 });
    expect(es.closed).toBe(true);
    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "loading" });

    // Request A returns afterward, with pre-reset state.
    stale.resolve(jsonResponse(200, { ...SCORE_BODY, total_points: 12345 }));
    await flush();
    await flush();

    // It must NOT restore the pre-reset state.
    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "loading" });

    unsubscribe();
  });
});

describe("signed-out / error propagation", () => {
  it("surfaces UNAUTHENTICATED from the score fetch as signed-out", async () => {
    const fetchMock = fetchRouter({
      "/api/attendees/me/score": () => errorResponse(401, "UNAUTHENTICATED"),
    });
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "signed-out" });
    unsubscribe();
  });

  it("surfaces other failures as an error state", async () => {
    const fetchMock = fetchRouter({
      "/api/attendees/me/score": () => errorResponse(503, "MAINTENANCE"),
    });
    vi.stubGlobal("fetch", fetchMock);
    const unsubscribe = realtimeStore.subscribeScore(() => {});
    MockEventSource.latest().emit("ready", { dataset_generation: 1, heartbeat_seconds: 15 });
    await flush();
    await flush();

    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "error", code: "MAINTENANCE" });
    unsubscribe();
  });
});
