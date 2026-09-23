import { describe, expect, it } from "vitest";

import { FALLBACK_MESSAGE, fieldErrorsFrom, messageFor } from "./errors";

describe("messageFor", () => {
  it("maps every M04 server code to attendee-facing copy", () => {
    for (const code of [
      "VALIDATION_ERROR",
      "UNKNOWN_INTEREST",
      "OTP_INVALID",
      "MAINTENANCE",
      "INTERNAL_ERROR",
      "NETWORK_ERROR",
    ]) {
      expect(messageFor(code)).not.toBe(FALLBACK_MESSAGE);
    }
  });

  it("falls back to a generic message for an unrecognised code (§20.4)", () => {
    expect(messageFor("SOMETHING_NEW_FROM_A_LATER_SERVER")).toBe(FALLBACK_MESSAGE);
  });

  it("gives one OTP message covering every indistinguishable cause", () => {
    const copy = messageFor("OTP_INVALID");
    for (const cause of ["incorrect", "expired", "already used", "too many attempts"]) {
      expect(copy).toContain(cause);
    }
  });
});

describe("fieldErrorsFrom", () => {
  it("maps VALIDATION_ERROR locations to per-field copy", () => {
    const result = fieldErrorsFrom({
      errors: [
        { loc: ["body", "linkedin_url"], msg: "Value error, raw server text", type: "value_error" },
        { loc: ["body", "sponsor_consent"], msg: "Field required", type: "missing" },
      ],
    });
    expect(Object.keys(result).sort()).toEqual(["linkedin_url", "sponsor_consent"]);
    expect(result.linkedin_url).not.toContain("raw server text");
  });

  it("ignores malformed details instead of throwing", () => {
    expect(fieldErrorsFrom({})).toEqual({});
    expect(fieldErrorsFrom({ errors: "nope" })).toEqual({});
    expect(fieldErrorsFrom({ errors: [null, { loc: "x" }, { loc: [] }] })).toEqual({});
  });
});
