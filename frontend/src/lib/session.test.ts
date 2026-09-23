import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchSession, logout } from "./session";

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

describe("fetchSession", () => {
  it("asks the server — the HttpOnly cookie is never read by this code", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, ATTENDEE));
    vi.stubGlobal("fetch", fetchMock);
    expect(await fetchSession()).toEqual({ kind: "signed-in", attendee: ATTENDEE });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me");
    expect(init.credentials).toBe("same-origin");
  });

  it.each([
    ["UNAUTHENTICATED", 401],
    ["REGISTRATION_NOT_VERIFIED", 403],
  ])("treats %s as signed out", async (code, status) => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(status, code)));
    expect(await fetchSession()).toEqual({ kind: "signed-out" });
  });

  it("surfaces anything else as an error rather than pretending to be signed out", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    const state = await fetchSession();
    expect(state.kind).toBe("error");
  });
});

describe("logout", () => {
  it("POSTs to the same-origin logout route and accepts an empty 204", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await logout()).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/auth/logout");
    expect(init.method).toBe("POST");
  });

  it("reports a CSRF rejection", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(403, "CSRF_REJECTED")));
    const result = await logout();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("CSRF_REJECTED");
  });
});
