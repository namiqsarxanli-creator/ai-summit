// Attendee session state (ARCHITECTURE.md §8.3). The session token lives
// in an HttpOnly cookie the browser sends automatically and this code can
// never read — so "am I signed in?" is answered by asking the server.

import { type ApiError, apiGet, apiPost } from "./api";

export type Attendee = {
  first_name: string;
  last_name: string;
  email: string;
  profession: string;
};

export type SessionState =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "signed-in"; attendee: Attendee }
  | { kind: "error"; error: ApiError };

export async function fetchSession(): Promise<SessionState> {
  const result = await apiGet<Attendee>("/api/attendees/me");
  if (result.ok) return { kind: "signed-in", attendee: result.data };
  // No, expired or revoked session (401) — or a session whose attendee
  // is no longer ACTIVE (403) — both simply mean "not signed in".
  if (result.error.code === "UNAUTHENTICATED" || result.error.code === "REGISTRATION_NOT_VERIFIED") {
    return { kind: "signed-out" };
  }
  return { kind: "error", error: result.error };
}

export async function logout(): Promise<{ ok: true } | { ok: false; error: ApiError }> {
  const result = await apiPost<null>("/api/auth/logout", {});
  return result.ok ? { ok: true } : { ok: false, error: result.error };
}
