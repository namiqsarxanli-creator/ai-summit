import { afterEach, describe, expect, it, vi } from "vitest";

import { NETWORK_ERROR, apiGet, apiPost } from "./api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("posts JSON to a same-origin relative path — never a backend origin", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { message: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiPost("/api/attendees/register", { a: 1 });

    expect(result).toEqual({ ok: true, data: { message: "ok" } });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/register");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe(JSON.stringify({ a: 1 }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("parses the §20.2 error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(400, {
          error: { code: "OTP_INVALID", message: "m", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await apiPost("/api/auth/otp/verify", {});
    expect(result).toEqual({
      ok: false,
      status: 400,
      error: { code: "OTP_INVALID", message: "m", details: {} },
    });
  });

  it("reports a failed fetch as the frontend-only NETWORK_ERROR", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const result = await apiGet("/api/interests");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe(NETWORK_ERROR);
  });

  it("treats an error without a recognisable envelope as INTERNAL_ERROR", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>502</html>", { status: 502 })));
    const result = await apiGet("/api/interests");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("INTERNAL_ERROR");
  });
});
