import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchPresets, fetchStaffMe, staffLogin, staffLogout } from "./staff";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchStaffMe", () => {
  it("calls the same-origin staff identity endpoint with credentials", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await fetchStaffMe();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/me");
    expect(init.credentials).toBe("same-origin");
  });
});

describe("fetchPresets", () => {
  it("calls the same-origin presets endpoint", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, []));
    vi.stubGlobal("fetch", fetchMock);
    await fetchPresets();
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/points/presets");
  });
});

describe("staffLogin", () => {
  it("posts email and password, same-origin, and never sends a booth id", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await staffLogin("staff@example.com", "hunter2");
    expect(result.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/auth/login");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ email: "staff@example.com", password: "hunter2" });
  });

  it("surfaces INVALID_CREDENTIALS without leaking which part was wrong", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(401, {
          error: { code: "INVALID_CREDENTIALS", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await staffLogin("staff@example.com", "wrong");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("INVALID_CREDENTIALS");
  });

  it("surfaces ACCOUNT_LOCKED", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(429, {
          error: { code: "ACCOUNT_LOCKED", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await staffLogin("staff@example.com", "wrong");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("ACCOUNT_LOCKED");
  });
});

describe("staffLogout", () => {
  it("POSTs to the same-origin staff logout route and accepts an empty 204", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await staffLogout();
    expect(result.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/auth/logout");
    expect(init.method).toBe("POST");
  });
});
