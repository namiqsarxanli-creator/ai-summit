// Staff / administrator passwords: changing one while signed in, and the emailed
// one-time reset. The server decides everything; this file only builds the
// requests and the client-side checks that save a pointless round trip.
//
// Nothing here ever stores, logs or echoes a password or a code: they live in the
// form's state until it is submitted, and are cleared on success.

import { type ApiError, apiPost } from "./api";

// The console's own rule for a staff password (backend initial_admin.py).
export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 256;

export type StaffResult = { ok: true } | { ok: false; error: ApiError };

// A message for a new password that cannot be sent, or "" when it can.
export function newPasswordProblem(next: string, confirmation: string): string {
  if (next.length < MIN_PASSWORD_LENGTH) return "The new password must be at least 12 characters.";
  if (next.length > MAX_PASSWORD_LENGTH) return "The new password is too long (at most 256 characters).";
  if (next !== confirmation) return "The two new passwords are not the same.";
  return "";
}

export function codeProblem(code: string): string {
  return /^\d{6}$/.test(code.trim()) ? "" : "Enter the 6-digit code from your email.";
}

function done(result: { ok: true; data: unknown } | { ok: false; error: ApiError }): StaffResult {
  return result.ok ? { ok: true } : { ok: false, error: result.error };
}

// Signed in: the CURRENT password proves who is at the keyboard. On success the
// server ends every session of the account, this one included.
export function changePassword(current: string, next: string, confirmation: string): Promise<StaffResult> {
  return apiPost<null>("/api/staff/auth/password", {
    current_password: current,
    new_password: next,
    new_password_confirmation: confirmation,
  }).then(done);
}

// "I forgot my password": the answer is the same whatever the address is.
export function requestPasswordReset(email: string, language: string): Promise<StaffResult> {
  return apiPost<unknown>("/api/staff/auth/password-reset/request", { email, language }).then(done);
}

export function confirmPasswordReset(email: string, code: string, next: string, confirmation: string): Promise<StaffResult> {
  return apiPost<null>("/api/staff/auth/password-reset/confirm", {
    email,
    code,
    new_password: next,
    new_password_confirmation: confirmation,
  }).then(done);
}

// An administrator makes the system email a reset code to the staff member's own
// address. There is no field for a password or a code: the administrator never
// sees either.
export function startStaffPasswordReset(staffId: number, language: string): Promise<StaffResult> {
  return apiPost<unknown>(`/api/admin/staff/${staffId}/password-reset`, { language }).then(done);
}

// Every UI text of the password screens, in one place so a test can check each one
// has an Azerbaijani and a Russian translation.
export const PASSWORD_TEXTS: string[] = [
  "Change password",
  "Change your password",
  "Current password",
  "New password",
  "Repeat the new password",
  "At least 12 characters.",
  "Enter your current password.",
  "The new password must be at least 12 characters.",
  "The new password is too long (at most 256 characters).",
  "The two new passwords are not the same.",
  "Changing password…",
  "Password changed",
  "Your password was changed. Every session of your account has ended, on every device. Sign in again with the new password.",
  "Sign in again",
  "Sign in first to change your password.",
  "Forgot your password?",
  "Reset your password",
  "We email a one-time code to your staff address. It is valid for 10 minutes.",
  "Send the code",
  "Sending…",
  "I already have a code",
  "Staff email",
  "If this address belongs to an active staff account, a code has been sent to it. It is valid for 10 minutes.",
  "Code from the email",
  "Enter the 6-digit code from your email.",
  "Enter your staff email address.",
  "Send a new code",
  "Set the new password",
  "Your password was reset. Sign in with the new password.",
  "Password reset",
  "Back to sign in",
  "If the password was already changed, sign in with the new one.",
  "Send password reset email",
  "Email language",
  "Staff ID for the reset email",
  "Send a password reset email to this staff member? They get a one-time code at their own address and choose the new password themselves; you will not see either.",
  "A reset email was queued for that staff member. You cannot see the code, and only they can choose the new password.",
  "Change my password",
];
