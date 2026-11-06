import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { ProfileView } from "./ProfileView";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const PROFILE = {
  attendee_id: 18,
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@example.com",
  phone: "+1-555-0142",
  profession: "Systems Engineer",
  linkedin_url: "https://www.linkedin.com/in/ada-demo",
  sponsor_consent: true,
  interests: [
    { slug: "generative-ai", name: "Generative AI" },
    { slug: "data-analytics", name: "Data Analytics" },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProfileView", () => {
  it("renders registration data and consent exactly as received", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, PROFILE)));
    render(<ProfileView />);
    expect(await screen.findByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument();
    expect(screen.getByText("Systems Engineer")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument(); // the number the Game Master filters by
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    expect(screen.getByText("+1-555-0142")).toBeInTheDocument();
    expect(screen.getByText("https://www.linkedin.com/in/ada-demo")).toBeInTheDocument();
    // Booth-recommendation interests are no longer a profile section (the
    // interest-card topics drive recommendations).
    expect(screen.queryByText("Your interests")).not.toBeInTheDocument();
    expect(screen.queryByText("Generative AI")).not.toBeInTheDocument();
    expect(
      screen.getByText("You agreed to share your details with event sponsors."),
    ).toBeInTheDocument();
    expect(screen.getByText("Private — visible only to you.")).toBeInTheDocument();
  });

  it("shows consent false and omits LinkedIn when absent, no phone number leaked as a fake consent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, { ...PROFILE, sponsor_consent: false, linkedin_url: null }),
      ),
    );
    render(<ProfileView />);
    await screen.findByRole("heading", { name: "Ada Lovelace" });
    expect(
      screen.getByText("You have not agreed to share your details with event sponsors."),
    ).toBeInTheDocument();
    expect(screen.queryByText("LinkedIn")).not.toBeInTheDocument();
  });

  it("shows no interests section, also when none are held", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ...PROFILE, interests: [] })));
    render(<ProfileView />);
    expect(await screen.findByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument();
    expect(screen.queryByText("You haven't selected any interests yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("Your interests")).not.toBeInTheDocument();
  });

  it.each([
    ["UNAUTHENTICATED", 401],
    ["REGISTRATION_NOT_VERIFIED", 403],
  ])("asks a signed-out visitor (%s) to sign in", async (code, status) => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(status, code)));
    render(<ProfileView />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<ProfileView />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });

  it("signs out, then confirms with the server that the session is gone", async () => {
    let signedIn = true;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/auth/logout") {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      return signedIn ? jsonResponse(200, PROFILE) : errorResponse(401, "UNAUTHENTICATED");
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([u]) => u);
    expect(urls).toEqual([
      "/api/attendees/me/profile",
      "/api/auth/logout",
      "/api/attendees/me/profile",
    ]);
  });

  it("keeps the profile shown and reports why if logout is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/api/auth/logout" ? errorResponse(403, "CSRF_REJECTED") : jsonResponse(200, PROFILE),
      ),
    );
    render(<ProfileView />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(messageFor("CSRF_REJECTED"))).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument(),
    );
  });

  it("never displays a raw phone number as the consent statement", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, PROFILE)));
    render(<ProfileView />);
    const consentSection = await screen.findByText(
      "You agreed to share your details with event sponsors.",
    );
    expect(consentSection.textContent).not.toContain("555");
  });
});
