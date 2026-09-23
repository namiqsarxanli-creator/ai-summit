import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "./page";

describe("Home", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/attendees/me"
          ? // The home page also resolves the session (M05); signed out here.
            new Response(
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
    vi.unstubAllGlobals();
  });

  it("renders the FACEMARK heading", () => {
    render(<Home />);
    expect(screen.getByRole("heading", { name: "FACEMARK" })).toBeInTheDocument();
  });

  it("calls the same-origin /api/health path, never a separate backend origin", async () => {
    render(<Home />);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/health"));
  });

  it("renders the health response once the fetch resolves", async () => {
    render(<Home />);
    await waitFor(() => expect(screen.getByText(/"status": "ok"/)).toBeInTheDocument());
  });
});
