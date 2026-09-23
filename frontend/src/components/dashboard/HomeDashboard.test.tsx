import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { HomeDashboard } from "./HomeDashboard";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("HomeDashboard", () => {
  it("shows only the top 3 matches, plus a See all link and an honest points placeholder", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, SCORED)));
    render(<HomeDashboard />);
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "FinPulse" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MediSync AI" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "SecureStack" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See all →" })).toHaveAttribute("href", "/booths");
    expect(
      screen.getByText("Points tracking starts once the summit opens — check back soon."),
    ).toBeInTheDocument();
    // No fabricated number stands in for the missing score.
    expect(screen.queryByText(/^\d+ pts?$/)).not.toBeInTheDocument();
  });

  it("shows the general-listing banner when interests are unset", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
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
