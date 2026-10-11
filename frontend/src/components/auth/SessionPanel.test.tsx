import { render, screen } from "@testing-library/react";
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
    expect(screen.getByRole("link", { name: "Staff sign in" })).toHaveAttribute("href", "/staff/login");
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
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Staff sign in" })).not.toBeInTheDocument();
    // M08: navigation to My Card and Booths now lives in the single
    // shared AttendeeTabBar (rendered by HomeDashboard), not duplicated
    // here as separate quick-link buttons.
    expect(screen.queryByRole("link", { name: "My card & QR" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Booth matches" })).not.toBeInTheDocument();
  });

  it("reports an unexpected failure but still offers the ways in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<SessionPanel />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Already registered? Sign in" })).toBeInTheDocument();
  });
});
