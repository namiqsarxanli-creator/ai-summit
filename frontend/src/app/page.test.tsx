import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { realtimeStore } from "@/lib/realtime";
import { installMockEventSource } from "@/test-utils/mockEventSource";

import Home from "./page";

describe("Home", () => {
  beforeEach(() => {
    // HomeDashboard's child ScoreSummary (M14) opens an EventSource on
    // mount; jsdom has no native implementation.
    installMockEventSource();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        // The home page also resolves the session (M05) and, for the
        // M08 dashboard preview, the attendee's recommendations (M07)
        // — signed out here, for both.
        url === "/api/attendees/me" || url === "/api/attendees/me/recommendations"
          ? new Response(
              JSON.stringify({
                error: { code: "UNAUTHENTICATED", message: "", details: {}, correlation_id: "c" },
              }),
              { status: 401, headers: { "Content-Type": "application/json" } },
            )
          : new Response(JSON.stringify({ status: "ok" }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
      )
    );
  });

  afterEach(() => {
    realtimeStore.resetForTests();
    vi.unstubAllGlobals();
  });

  it("renders the AI Summit heading", () => {
    render(<Home />);
    expect(screen.getByRole("heading", { name: "AI Summit" })).toBeInTheDocument();
  });

  it("presents event actions without developer health data", async () => {
    render(<Home />);
    expect(screen.getByText("People. Ideas. Real impact.")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Register as an attendee/ })).toBeInTheDocument();
    expect(screen.queryByText(/Backend connectivity/)).not.toBeInTheDocument();
  });

  it("shows the profile summary, matched-booths preview and tab bar together when signed in", async () => {
    const ATTENDEE = {
      first_name: "Marcus",
      last_name: "Lindqvist",
      email: "marcus.lindqvist@attendee.facemark.demo",
      profession: "Data Platform Engineer",
    };
    const RECS = [
      {
        booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" },
        match: {
          score: 78,
          matched_interests: [{ slug: "generative-ai", name: "Generative AI" }],
          explanation: "Good match based on your interests in Generative AI.",
          algorithm_version: "weighted-v1",
        },
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/attendees/me") return new Response(JSON.stringify(ATTENDEE), { status: 200 });
        if (url === "/api/attendees/me/recommendations") {
          return new Response(JSON.stringify(RECS), { status: 200 });
        }
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }),
    );
    render(<Home />);
    // M08: the profile summary (M05's SessionPanel) ...
    expect(await screen.findByText("Marcus Lindqvist")).toBeInTheDocument();
    // ... the matched-booths preview (M07 data, this milestone's UI) ...
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    // ... and the shared attendee navigation, all on one coherent screen.
    expect(screen.getByRole("navigation", { name: "Attendee navigation" })).toBeInTheDocument();
  });
});
