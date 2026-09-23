import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchProfile } from "./profile";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchProfile", () => {
  it("calls the same-origin private profile endpoint with credentials", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchProfile();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/profile");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
  });
});
