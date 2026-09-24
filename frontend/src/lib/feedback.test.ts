import { afterEach, describe, expect, it, vi } from "vitest";

import { submitFeedback } from "./feedback";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("submitFeedback", () => {
  it("posts exactly booth_slug/rating/comment, same-origin, and returns the outcome", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        feedback_id: 91,
        awarded_points: 5,
        already_rewarded: false,
        total_points: 50,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const request = { booth_slug: "aiworks", rating: 5, comment: "Great booth!" };
    const result = await submitFeedback(request);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/feedback");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual(request);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.already_rewarded).toBe(false);
      expect(result.data.total_points).toBe(50);
    }
  });

  it("surfaces the already-rewarded outcome as a normal 200, not an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          feedback_id: 92,
          awarded_points: 0,
          already_rewarded: true,
          total_points: 50,
        }),
      ),
    );
    const result = await submitFeedback({ booth_slug: "aiworks", rating: 3 });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.already_rewarded).toBe(true);
      expect(result.data.awarded_points).toBe(0);
    }
  });

  it("surfaces REGISTRATION_NOT_VERIFIED for a provisional attendee", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(403, {
          error: {
            code: "REGISTRATION_NOT_VERIFIED",
            message: "",
            details: {},
            correlation_id: "c",
          },
        }),
      ),
    );
    const result = await submitFeedback({ booth_slug: "aiworks", rating: 5 });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("REGISTRATION_NOT_VERIFIED");
  });
});
