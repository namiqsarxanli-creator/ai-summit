import { afterEach, describe, expect, it, vi } from "vitest";

import { hasTranslation } from "./i18n";
import { playgroundTranslations } from "./i18n-playground";
import { STATUS_LABEL, acceptPlayground, cancelPlayground, invitationLink, playgroundError } from "./playground";

afterEach(() => vi.unstubAllGlobals());

describe("playground requests", () => {
  it("send the version on cancellation and the opaque invitation in the body", async () => {
    const fetcher = vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetcher);
    await cancelPlayground({ id: 4, slot_id: 1, status: "confirmed", version: 3, expires_at: null });
    expect(fetcher.mock.calls[0]).toMatchObject(["/api/playground/bookings/4/cancel", { body: JSON.stringify({ expected_version: 3 }) }]);
    await acceptPlayground("opaque-token");
    expect(fetcher.mock.calls[1]).toMatchObject(["/api/playground/invitations/accept", { body: JSON.stringify({ token: "opaque-token" }) }]);
  });

  it("put the invitation in the fragment, never in a query string", () => {
    expect(invitationLink("https://summit.example", "12.ab+c")).toBe("https://summit.example/playground#invite=12.ab%2Bc");
  });
});

describe("playground texts", () => {
  it("name the limit and fall back plainly for unknown errors", () => {
    expect(playgroundError("active_booking_limit")).toContain("3 active");
    expect(playgroundError("future_reason")).toContain("Refresh");
  });

  it("have Azerbaijani and Russian for every status and keep placeholders", () => {
    for (const label of Object.values(STATUS_LABEL)) expect(hasTranslation(label)).toBe(true);
    for (const [key, value] of Object.entries(playgroundTranslations)) {
      for (const token of key.match(/\{[a-z]+\}/g) ?? []) {
        expect(value.az).toContain(token);
        expect(value.ru).toContain(token);
      }
    }
  });
});
