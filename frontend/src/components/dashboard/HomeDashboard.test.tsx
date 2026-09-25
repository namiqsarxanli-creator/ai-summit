import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";
import { realtimeStore } from "@/lib/realtime";
import { installMockEventSource, MockEventSource } from "@/test-utils/mockEventSource";

import { HomeDashboard } from "./HomeDashboard";

// The child `ScoreSummary` (M14) is driven by the shared realtime
// store: mounting it opens an EventSource, and its score fetch only
// happens once `ready` arrives.
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

const SCORE = { total_points: 45, rank: 3, recent_awards: [], as_of: "2026-09-22T17:04:11Z", dataset_generation: 7 };

// Recommendations and score are two independent fetches once signed
// in (HomeDashboard's own `fetchRecommendations`, and its child
// `ScoreSummary`'s `fetchMyScore`) — routed by URL like
// BoothDetailView.test.tsx's `signedOutRouter`.
function router(recommendationsResponse: () => Response, scoreResponse: () => Response = () => jsonResponse(200, SCORE)) {
  return vi.fn(async (url: string) => {
    if (url === "/api/attendees/me/score") return scoreResponse();
    return recommendationsResponse();
  });
}

const SCORED = [
  {
    booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" },
    match: {
      score: 78,
      matched_interests: [{ slug: "generative-ai", name: "Generative AI" }],
      explanation: "Good match based on your interests in Generative AI.",
      algorithm_version: "weighted-v1",
    },
  },
  {
    booth: { slug: "finpulse", name: "FinPulse", location_label: "Hall A · 11" },
    match: {
      score: 34,
      matched_interests: [{ slug: "data-analytics", name: "Data Analytics" }],
      explanation: "Some overlap based on your interests in Data Analytics.",
      algorithm_version: "weighted-v1",
    },
  },
  {
    booth: { slug: "medisync-ai", name: "MediSync AI", location_label: "Hall B · 6" },
    match: {
      score: 29,
      matched_interests: [{ slug: "data-analytics", name: "Data Analytics" }],
      explanation: "Some overlap based on your interests in Data Analytics.",
      algorithm_version: "weighted-v1",
    },
  },
  {
    booth: { slug: "securestack", name: "SecureStack", location_label: "Hall C · 8" },
    match: {
      score: 24,
      matched_interests: [{ slug: "data-analytics", name: "Data Analytics" }],
      explanation: "Some overlap based on your interests in Data Analytics.",
      algorithm_version: "weighted-v1",
    },
  },
];

beforeEach(() => {
  installMockEventSource();
});

afterEach(() => {
  realtimeStore.resetForTests();
  vi.unstubAllGlobals();
});

describe("HomeDashboard", () => {
  it("shows only the top 3 matches, plus a See all link and the real score/rank", async () => {
    vi.stubGlobal("fetch", router(() => jsonResponse(200, SCORED)));
    render(<HomeDashboard />);
    // ScoreSummary (and its EventSource) only mounts once HomeDashboard's
    // own recommendations fetch resolves into the "ok" branch.
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
    openReady();
    expect(screen.getByRole("heading", { name: "FinPulse" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MediSync AI" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "SecureStack" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See all →" })).toHaveAttribute("href", "/booths");
    // M13 wires the real, server-computed score/rank in place of M08's
    // placeholder — see ScoreSummary.test.tsx for its own detailed cases.
    expect(await screen.findByText("45")).toBeInTheDocument();
    expect(screen.getByText("Rank #3")).toBeInTheDocument();
  });

  it("shows the general-listing banner when interests are unset", async () => {
    vi.stubGlobal(
      "fetch",
      router(() =>
        jsonResponse(200, [
          { booth: { slug: "aiworks", name: "AIWorks", location_label: null }, match: null },
        ]),
      ),
    );
    render(<HomeDashboard />);
    await screen.findByRole("heading", { name: "AIWorks" });
    expect(
      screen.getByText(
        "Select interests when you register to see your personalised match for each booth.",
      ),
    ).toBeInTheDocument();
  });

  it("renders nothing when signed out — SessionPanel already offers the CTA", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    const { container } = render(<HomeDashboard />);
    await vi.waitFor(() => expect(container.textContent).toBe(""));
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<HomeDashboard />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("shows a loading state before data arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<HomeDashboard />);
    expect(screen.getByText("Loading your dashboard…")).toBeInTheDocument();
  });
});
