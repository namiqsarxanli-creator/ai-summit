import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { Recommendations } from "./Recommendations";

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
];

const GENERAL = [
  { booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" }, match: null },
  { booth: { slug: "finpulse", name: "FinPulse", location_label: "Hall A · 11" }, match: null },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Recommendations", () => {
  it("renders a personalized, ranked list without a 'select interests' banner", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, SCORED)));
    render(<Recommendations />);
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "FinPulse" })).toBeInTheDocument();
    expect(screen.getByLabelText("78% match")).toBeInTheDocument();
    expect(screen.getByLabelText("34% match")).toBeInTheDocument();
    expect(screen.getByText("Ranked for you, based on the interests you selected.")).toBeInTheDocument();
  });

  it("renders the general listing with an explanatory banner and no scores when interests are unset", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, GENERAL)));
    render(<Recommendations />);
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/% match/)).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Choose topics on your interest card in your profile to see your personalised match for each booth. For now, here are all the booths at the summit.",
      ),
    ).toBeInTheDocument();
  });

  it("shows an empty state when there are no booths at all", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, [])));
    render(<Recommendations />);
    expect(await screen.findByText("No booths are available yet. Check back soon.")).toBeInTheDocument();
  });

  it.each([
    ["UNAUTHENTICATED", 401],
    ["REGISTRATION_NOT_VERIFIED", 403],
  ])("asks a signed-out visitor (%s) to sign in", async (code, status) => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(status, code)));
    render(<Recommendations />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<Recommendations />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("reports a network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    render(<Recommendations />);
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });

  it("fetches from the session-scoped endpoint with no attendee id anywhere", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, SCORED));
    vi.stubGlobal("fetch", fetchMock);
    render(<Recommendations />);
    await screen.findByRole("heading", { name: "AIWorks" });
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/recommendations");
  });
});
