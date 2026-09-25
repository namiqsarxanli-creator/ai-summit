import { afterEach, describe, expect, it, vi } from "vitest";

import { extractCardToken, fetchMyCard, fetchPublicCard, isCardUrl, safeExternalUrl } from "./card";

const TOKEN = "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isCardUrl — the only thing ever encoded into a QR (§9.2)", () => {
  it.each([`https://summit.example/c/${TOKEN}`, `http://localhost:3000/c/${TOKEN}`])(
    "accepts %s",
    (url) => expect(isCardUrl(url)).toBe(true),
  );

  it.each([
    [`https://summit.example/c/${TOKEN}?email=a@b.c`, "query string"],
    [`https://summit.example/c/${TOKEN}#1`, "fragment"],
    [`https://summit.example/c/${TOKEN}/42`, "extra path"],
    [`https://summit.example/c/42`, "internal id instead of a token"],
    [`https://summit.example/c/${TOKEN.slice(1)}`, "short token"],
    [`https://user@summit.example/c/${TOKEN}`, "userinfo"],
    [`https://summit.example/u/${TOKEN}`, "wrong path"],
    [`javascript:alert(1)//c/${TOKEN}`, "non-http scheme"],
    [`{"id":42,"email":"a@b.c"}`, "JSON payload"],
  ])("rejects %s (%s)", (url) => expect(isCardUrl(url)).toBe(false));
});

describe("extractCardToken — M10 scanner input", () => {
  it.each([`https://summit.example/c/${TOKEN}`, `http://localhost:3000/c/${TOKEN}`])(
    "extracts the token from %s",
    (url) => expect(extractCardToken(url)).toBe(TOKEN),
  );

  it.each([
    ["https://example.com/not-facemark", "an arbitrary external QR"],
    [`https://summit.example/c/${TOKEN}?email=a@b.c`, "query string"],
    [`https://summit.example/c/${TOKEN.slice(1)}`, "short token"],
    [`https://summit.example/u/${TOKEN}`, "wrong path"],
    ["not even a url", "malformed text"],
    ['{"id":42,"email":"a@b.c"}', "JSON payload"],
  ])("returns null for %s (%s)", (value) => expect(extractCardToken(value)).toBeNull());
});

describe("safeExternalUrl", () => {
  it("allows http(s) LinkedIn links and nothing else", () => {
    expect(safeExternalUrl("https://www.linkedin.com/in/ada")).toBe("https://www.linkedin.com/in/ada");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,hi")).toBeNull();
    expect(safeExternalUrl("https://linkedin.com.evil.example/in/ada")).toBeNull();
    expect(safeExternalUrl("https://linkedin.com@evil.example/in/ada")).toBeNull();
  });
});

describe("card API calls", () => {
  it("reads the public card by token, same-origin, with no identity attached", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchPublicCard(TOKEN);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/cards/${TOKEN}`);
    expect(init.method).toBe("GET");
  });

  it("asks for MY card with no user id — the session cookie decides", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchMyCard();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/card");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBeUndefined();
  });
});
