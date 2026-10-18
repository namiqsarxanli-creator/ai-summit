import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { StaffPanel } from "./StaffPanel";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const BOOTH_STAFF_ME = { full_name: "Noah Bergström", role: "BOOTH_STAFF", booth_name: "AIWorks" };
const ADMIN_ME = { full_name: "Jordan Blake", role: "ADMIN", booth_name: null };
const TASKS = [
  { id: 1, title: "Watch the live demo", description: null, points: 10, repeatable: false },
  { id: 2, title: "Ask about deployment", description: null, points: 5, repeatable: true },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StaffPanel", () => {
  it("shows the signed-in staff identity and booth, derived from the server, plus the activities and their points", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/staff/me" ? jsonResponse(200, BOOTH_STAFF_ME) : jsonResponse(200, TASKS),
      ),
    );
    render(<StaffPanel />);
    expect(await screen.findByText("Noah Bergström")).toBeInTheDocument();
    expect(screen.getByText("AIWorks")).toBeInTheDocument();
    expect(screen.getByText("Watch the live demo · +10")).toBeInTheDocument();
    expect(screen.getByText("Ask about deployment · +5")).toBeInTheDocument();
  });

  it("shows an admin with no booth and no activities section", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, ADMIN_ME));
    vi.stubGlobal("fetch", fetchMock);
    render(<StaffPanel />);
    expect(await screen.findByText("Jordan Blake")).toBeInTheDocument();
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Organiser panel" })).toHaveAttribute("href", "/admin");
    expect(screen.queryByLabelText("Activities")).not.toBeInTheDocument();
    // Only /me was ever called — never /staff/tasks for an admin.
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([u]) => u)).toEqual(["/api/staff/me"]);
  });

  it("offers scanning (M10) but no award controls yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/staff/me" ? jsonResponse(200, BOOTH_STAFF_ME) : jsonResponse(200, TASKS),
      ),
    );
    render(<StaffPanel />);
    await screen.findByText("Noah Bergström");
    expect(screen.getByRole("link", { name: "Scan attendee" })).toHaveAttribute(
      "href",
      "/staff/scan",
    );
    for (const preset of TASKS) {
      expect(
        screen.queryByRole("button", { name: new RegExp(`\\+${preset.points}`) }),
      ).not.toBeInTheDocument();
    }
  });

  it.each([
    ["UNAUTHENTICATED", 401],
    ["STAFF_NOT_AUTHORIZED", 403],
  ])("offers a sign-in link when not authenticated (%s)", async (code, status) => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(status, code)));
    render(<StaffPanel />);
    expect(await screen.findByRole("link", { name: "Staff sign in" })).toHaveAttribute(
      "href",
      "/staff/login",
    );
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<StaffPanel />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("shows an activities error without pretending none are configured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/staff/me" ? jsonResponse(200, BOOTH_STAFF_ME) : errorResponse(503, "MAINTENANCE"),
      ),
    );
    render(<StaffPanel />);
    await screen.findByText("Noah Bergström");
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
    expect(
      screen.queryByText("No activities with a point value are set up for this booth yet."),
    ).not.toBeInTheDocument();
  });

  it("links to the page where the signed-in person changes their own password", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url === "/api/staff/me" ? jsonResponse(200, BOOTH_STAFF_ME) : jsonResponse(200, TASKS))));
    render(<StaffPanel />);
    expect(await screen.findByRole("link", { name: "Change password" })).toHaveAttribute("href", "/staff/password");
  });

  it("signs out, then confirms the panel shows the sign-in link", async () => {
    let signedIn = true;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/logout") {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      if (!signedIn) return errorResponse(401, "UNAUTHENTICATED");
      return url === "/api/staff/me" ? jsonResponse(200, BOOTH_STAFF_ME) : jsonResponse(200, TASKS);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<StaffPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("link", { name: "Staff sign in" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/auth/logout")).toBe(true);
  });
});
