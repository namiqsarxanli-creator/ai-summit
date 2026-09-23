import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { SessionPanel } from "./SessionPanel";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const ATTENDEE = {
  first_name: "Marcus",
  last_name: "Lindqvist",
  email: "marcus.lindqvist@attendee.facemark.demo",
  profession: "Data Platform Engineer",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SessionPanel", () => {
  it("offers register and sign-in when there is no session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    render(<SessionPanel />);
    expect(await screen.findByRole("link", { name: "Register as an attendee" })).toHaveAttribute(
      "href",
      "/register",
    );
    expect(screen.getByRole("link", { name: "Already registered? Sign in" })).toHaveAttribute(
      "href",
      "/login",
    );
  });

  it("shows the signed-in attendee resolved from the server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, ATTENDEE)));
    render(<SessionPanel />);
    expect(await screen.findByText("Marcus Lindqvist")).toBeInTheDocument();
    expect(screen.getByText("Data Platform Engineer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    // M06: the way to the attendee's own card — no id in the link.
    expect(screen.getByRole("link", { name: "My card & QR" })).toHaveAttribute("href", "/my-card");
    // M07: the way to the attendee's personalised booth matches.
    expect(screen.getByRole("link", { name: "Booth matches" })).toHaveAttribute("href", "/booths");
  });

  it("offers no card or booth-match link when signed out", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    render(<SessionPanel />);
    await screen.findByRole("link", { name: "Register as an attendee" });
    expect(screen.queryByRole("link", { name: "My card & QR" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Booth matches" })).not.toBeInTheDocument();
  });

  it("signs out, then confirms with the server that the session is gone", async () => {
    let signedIn = true;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/auth/logout") {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      return signedIn ? jsonResponse(200, ATTENDEE) : errorResponse(401, "UNAUTHENTICATED");
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("link", { name: "Register as an attendee" })).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([u]) => u);
    expect(urls).toEqual(["/api/attendees/me", "/api/auth/logout", "/api/attendees/me"]);
  });

  it("keeps the attendee signed in and says why if logout is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/auth/logout" ? errorResponse(403, "CSRF_REJECTED") : jsonResponse(200, ATTENDEE),
      ),
    );
    render(<SessionPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(messageFor("CSRF_REJECTED"))).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Marcus Lindqvist")).toBeInTheDocument());
  });

  it("reports an unexpected failure but still offers the ways in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<SessionPanel />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Already registered? Sign in" })).toBeInTheDocument();
  });
});
