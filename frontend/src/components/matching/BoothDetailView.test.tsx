import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { BoothDetailView } from "./BoothDetailView";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

// The signed-out response the FeedbackForm's own session check expects
// — this view composes it but the assertions below are about the
// booth data, so every test here is signed-out unless it says otherwise.
function signedOutRouter(boothResponse: () => Response) {
  return vi.fn(async (url: string) => {
    if (url === "/api/attendees/me") return errorResponse(401, "UNAUTHENTICATED");
    return boothResponse();
  });
}

const AIWORKS = {
  slug: "aiworks",
  name: "AIWorks",
  description: "Enterprise generative AI copilots.",
  location_label: "Hall B · 14",
  tasks: [
    { title: "Watch the live generative AI demo", description: null },
    { title: "Ask about enterprise deployment", description: "Talk to the team about rollout." },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BoothDetailView", () => {
  it("renders name, location, description and tasks from the API, needing no session", async () => {
    const fetchMock = signedOutRouter(() => jsonResponse(200, AIWORKS));
    vi.stubGlobal("fetch", fetchMock);
    render(<BoothDetailView slug="aiworks" />);
    expect(await screen.findByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    expect(screen.getByText("Hall B · 14")).toBeInTheDocument();
    expect(screen.getByText("Enterprise generative AI copilots.")).toBeInTheDocument();
    expect(screen.getByText("Watch the live generative AI demo")).toBeInTheDocument();
    expect(screen.getByText("Ask about enterprise deployment")).toBeInTheDocument();
    expect(screen.getByText("Talk to the team about rollout.")).toBeInTheDocument();
    // No point value anywhere on a task (§10, M08: not this screen's data).
    expect(screen.queryByText(/\bpts\b|\bpoints\b/i)).not.toBeInTheDocument();

    expect(fetchMock.mock.calls.some(([u]) => u === "/api/booths/aiworks")).toBe(true);
  });

  it("shows a neutral message when the booth has no listed tasks", async () => {
    vi.stubGlobal("fetch", signedOutRouter(() => jsonResponse(200, { ...AIWORKS, tasks: [] })));
    render(<BoothDetailView slug="aiworks" />);
    expect(
      await screen.findByText("No activities are listed for this booth yet."),
    ).toBeInTheDocument();
  });

  it("shows the one INVALID_QR-adjacent 404 message for an unknown booth", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(404, {
          error: { code: "BOOTH_NOT_FOUND", message: "server text", details: {}, correlation_id: "c" },
        }),
      ),
    );
    render(<BoothDetailView slug="nope" />);
    expect(await screen.findByText(messageFor("BOOTH_NOT_FOUND"))).toBeInTheDocument();
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
  });
});
