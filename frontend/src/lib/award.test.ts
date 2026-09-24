import { afterEach, describe, expect, it, vi } from "vitest";

import { awardPoints } from "./award";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("awardPoints", () => {
  it("posts exactly the canonical fields, same-origin, and returns the snapshot", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        points_log_id: 481,
        points_awarded: 15,
        resulting_total: 45,
        attendee_name: "Leyla A.",
        booth_name: "AIWorks",
        awarded_at: "2026-09-22T17:04:11Z",
        replayed: false,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const request = {
      card_token: "tok",
      points: 15,
      idempotency_key: "b3b1e6a0-0000-4000-8000-000000000000",
      scan_ref: "abc.def",
    };
    const result = await awardPoints(request);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/points/award");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual(request);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.resulting_total).toBe(45);
      expect(result.data.replayed).toBe(false);
    }
  });

  it("surfaces INVALID_POINT_VALUE", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(400, {
          error: { code: "INVALID_POINT_VALUE", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await awardPoints({
      card_token: "tok",
      points: 12,
      idempotency_key: "b3b1e6a0-0000-4000-8000-000000000000",
      scan_ref: "abc.def",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("INVALID_POINT_VALUE");
  });

  it("surfaces DUPLICATE_TASK_REWARD", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(409, {
          error: { code: "DUPLICATE_TASK_REWARD", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await awardPoints({
      card_token: "tok",
      points: 5,
      idempotency_key: "b3b1e6a0-0000-4000-8000-000000000000",
      scan_ref: "abc.def",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("DUPLICATE_TASK_REWARD");
  });
});
