import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchBoothDetail,
  fetchBoothListing,
  fetchBooths,
  fetchRecommendations,
  isGeneralListing,
  type Recommendation,
} from "./matching";

afterEach(() => {
  vi.unstubAllGlobals();
});

const SCORED: Recommendation = {
  booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" },
  match: {
    score: 78,
    matched_interests: [{ slug: "generative-ai", name: "Generative AI" }],
    explanation: "Good match based on your interests in Generative AI.",
    algorithm_version: "weighted-v1",
  },
};

const UNSCORED: Recommendation = {
  booth: { slug: "finpulse", name: "FinPulse", location_label: "Hall A · 11" },
  match: null,
};

describe("fetchRecommendations", () => {
  it("calls the same-origin recommendations endpoint with credentials", async () => {
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchRecommendations();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/recommendations");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
  });
});

describe("fetchBooths", () => {
  it("calls the same-origin booth catalogue endpoint", async () => {
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchBooths();
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/booths");
  });
});

describe("fetchBoothDetail", () => {
  it("calls the same-origin booth detail endpoint, slug URL-encoded", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchBoothDetail("some slug/weird");
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/booths/some%20slug%2Fweird");
  });
});

describe("isGeneralListing", () => {
  it("is true only when every entry has a null match", () => {
    expect(isGeneralListing([UNSCORED, UNSCORED])).toBe(true);
  });

  it("is false when every entry is scored", () => {
    expect(isGeneralListing([SCORED, SCORED])).toBe(false);
  });

  it("is false for an empty list — there is nothing to call general", () => {
    expect(isGeneralListing([])).toBe(false);
  });

  it("does not attempt to be true for a mixed list (the server never sends one)", () => {
    expect(isGeneralListing([SCORED, UNSCORED])).toBe(false);
  });
});

describe("fetchBoothListing", () => {
  const respond = (recs: unknown, booths: unknown, boothStatus = 200) =>
    vi.fn(async (url: string) =>
      url === "/api/booths"
        ? new Response(JSON.stringify(booths), { status: boothStatus, headers: { "Content-Type": "application/json" } })
        : new Response(JSON.stringify(recs), { status: 200, headers: { "Content-Type": "application/json" } }));

  it("lists every active booth: the matches first, then the booths without an interest profile", async () => {
    vi.stubGlobal("fetch", respond([SCORED], [UNSCORED.booth, SCORED.booth]));
    const r = await fetchBoothListing();
    expect(r.ok && r.data.map((x) => [x.booth.slug, x.match?.score ?? null])).toEqual([["aiworks", 78], ["finpulse", null]]);
  });

  it("is not empty when no booth has an interest profile yet", async () => {
    vi.stubGlobal("fetch", respond([], [UNSCORED.booth]));
    const r = await fetchBoothListing();
    expect(r.ok && r.data).toEqual([UNSCORED]);
  });

  it("keeps the recommendations when the catalogue cannot be read", async () => {
    vi.stubGlobal("fetch", respond([SCORED], { error: { code: "MAINTENANCE" } }, 503));
    const r = await fetchBoothListing();
    expect(r.ok && r.data).toEqual([SCORED]);
  });
});
