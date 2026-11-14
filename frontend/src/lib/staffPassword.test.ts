import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "./errors";
import { hasTranslation, translate } from "./i18n";
import {
  MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, PASSWORD_TEXTS, changePassword, codeProblem, confirmPasswordReset,
  newPasswordProblem, requestPasswordReset, startStaffPasswordReset,
} from "./staffPassword";

afterEach(() => { vi.unstubAllGlobals(); });

function stubFetch() {
  const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
  return () => {
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    return { url, method: init.method, body: JSON.parse(String(init.body)) as unknown };
  };
}

describe("password rules", () => {
  it("uses the console's own length rule and needs the password twice", () => {
    expect(MIN_PASSWORD_LENGTH).toBe(12);
    expect(MAX_PASSWORD_LENGTH).toBe(256);
    expect(newPasswordProblem("x".repeat(11), "x".repeat(11))).not.toBe("");
    expect(newPasswordProblem("x".repeat(12), "x".repeat(12))).toBe("");
    expect(newPasswordProblem("x".repeat(256), "x".repeat(256))).toBe("");
    expect(newPasswordProblem("x".repeat(257), "x".repeat(257))).not.toBe("");
    expect(newPasswordProblem("a".repeat(12), "b".repeat(12))).toBe("The two new passwords are not the same.");
    expect(newPasswordProblem("p@ss w0rd  ok!", "p@ss w0rd  ok!")).toBe(""); // spaces are part of a password: never trimmed
  });

  it("checks a reset code is six digits", () => {
    expect(codeProblem("123456")).toBe("");
    expect(codeProblem(" 123456 ")).toBe("");
    for (const bad of ["", "12345", "1234567", "12a456", "１２３４５６"]) expect(codeProblem(bad)).not.toBe("");
  });
});

describe("password requests", () => {
  it("change: the current and the new password twice, to the staff endpoint", async () => {
    const sent = stubFetch();
    await changePassword("old", "new-password-123", "new-password-123");
    expect(sent()).toEqual({
      url: "/api/staff/auth/password", method: "POST",
      body: { current_password: "old", new_password: "new-password-123", new_password_confirmation: "new-password-123" },
    });
  });

  it("reset request and confirmation", async () => {
    let sent = stubFetch();
    await requestPasswordReset("a@b.co", "az");
    expect(sent()).toEqual({ url: "/api/staff/auth/password-reset/request", method: "POST", body: { email: "a@b.co", language: "az" } });
    vi.unstubAllGlobals();
    sent = stubFetch();
    await confirmPasswordReset("a@b.co", "123456", "new-password-123", "new-password-123");
    expect(sent().body).toEqual({ email: "a@b.co", code: "123456", new_password: "new-password-123", new_password_confirmation: "new-password-123" });
  });

  it("an administrator's request carries only the language: no password, no code", async () => {
    const sent = stubFetch();
    await startStaffPasswordReset(7, "ru");
    expect(sent()).toEqual({ url: "/api/admin/staff/7/password-reset", method: "POST", body: { language: "ru" } });
  });
});

describe("password texts", () => {
  it("every text of the screens, and every error they can show, has an Azerbaijani and a Russian translation", () => {
    const errors = ["CURRENT_PASSWORD_INVALID", "PASSWORD_UNCHANGED", "PASSWORD_RESET_UNAVAILABLE", "OTP_INVALID", "ACCOUNT_LOCKED", "RATE_LIMITED", "UNAUTHENTICATED"].map(messageFor);
    const extra = ["Reset your password", "Changing it ends every session of your account, on every device.", "Forgot your password?", "Staff sign in"];
    const missing = [...new Set([...PASSWORD_TEXTS, ...errors, ...extra])].filter((text) => !hasTranslation(text));
    expect(missing).toEqual([]);
    for (const text of ["Change password", "Forgot your password?", "Send the code"]) {
      expect(translate(text, "az")).not.toBe(text);
      expect(translate(text, "ru")).not.toBe(text);
    }
  });
});
