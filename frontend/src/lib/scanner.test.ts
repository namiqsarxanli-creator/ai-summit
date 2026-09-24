import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveScan, tokenFromScannedText } from "./scanner";

const TOKEN = "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";

describe("tokenFromScannedText", () => {
  it("extracts the token from a decoded card URL", () => {
    expect(tokenFromScannedText(`https://summit.example/c/${TOKEN}`)).toBe(TOKEN);
  });

  it("accepts a bare token as typed by the manual-entry fallback", () => {
    expect(tokenFromScannedText(TOKEN)).toBe(TOKEN);
    expect(tokenFromScannedText(`  ${TOKEN}  `)).toBe(TOKEN);
  });

  it("rejects an arbitrary external QR's payload", () => {
    expect(tokenFromScannedText("https://example.com/not-facemark")).toBeNull();
  });

  it("rejects malformed or short input", () => {
    expect(tokenFromScannedText("not a token")).toBeNull();
    expect(tokenFromScannedText(TOKEN.slice(1))).toBeNull();
    expect(tokenFromScannedText("")).toBeNull();
  });
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveScan", () => {
  it("posts the card token, same-origin, and returns the identity plus scan_ref", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, {
        first_name: "Marcus",
        last_name: "Lindqvist",
        profession: "Data Platform Engineer",
        scan_ref: "abc.def",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await resolveScan("some-card-token");

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/scan/resolve");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual({ card_token: "some-card-token" });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.scan_ref).toBe("abc.def");
  });

  it("surfaces INVALID_QR for an unresolvable token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(404, {
          error: { code: "INVALID_QR", message: "", details: {}, correlation_id: "c" },
        }),
      ),
    );
    const result = await resolveScan("bad-token");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe("INVALID_QR");
  });
});
