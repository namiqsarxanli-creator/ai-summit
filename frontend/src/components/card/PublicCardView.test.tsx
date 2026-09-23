import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { PublicCardView } from "./PublicCardView";

const TOKEN = "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PublicCardView", () => {
  it("fetches the card for the token in the URL and shows it", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        first_name: "Marcus",
        last_name: "Lindqvist",
        profession: "Data Platform Engineer",
        interests: [{ slug: "generative-ai", name: "Generative AI" }],
        contact: { type: "EMAIL", value: "marcus.lindqvist@attendee.facemark.demo" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<PublicCardView token={TOKEN} />);
    expect(await screen.findByRole("heading", { name: "Marcus Lindqvist" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Email Marcus" })).toBeInTheDocument();
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/cards/${TOKEN}`);
  });

  it("shows the one §9.4 message for any invalid token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(404, {
          error: { code: "INVALID_QR", message: "server text", details: {}, correlation_id: "c" },
        }),
      ),
    );
    render(<PublicCardView token="nope" />);
    expect(
      await screen.findByText("This QR code isn't valid. Ask the attendee to reopen their card."),
    ).toBeInTheDocument();
    expect(messageFor("INVALID_QR")).toBe(
      "This QR code isn't valid. Ask the attendee to reopen their card.",
    );
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
  });

  it("reports a network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    render(<PublicCardView token={TOKEN} />);
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });
});
