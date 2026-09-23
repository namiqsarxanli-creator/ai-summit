import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchBooths, fetchRecommendations, isGeneralListing, type Recommendation } from "./matching";

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
