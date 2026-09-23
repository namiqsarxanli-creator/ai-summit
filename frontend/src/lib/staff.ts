// Staff session state (ARCHITECTURE.md §12, IMPLEMENTATION_PLAN.md
// M09). A DIFFERENT principal from the attendee session in
// `lib/session.ts` — this code never reads or writes the attendee
// endpoints, and vice versa (§12.2, §24.3: booth authority is derived
// server-side from THIS session, never supplied by the client).

import { type ApiError, apiGet, apiPost } from "./api";

export type StaffRole = "BOOTH_STAFF" | "ADMIN";

export type StaffMe = {
  full_name: string;
  role: StaffRole;
  // `null` for ADMIN — an admin has no booth (§7.7's CHECK constraint).
  booth_name: string | null;
};

export type Preset = { points: number; label: string | null };

export function fetchStaffMe(): Promise<{ ok: true; data: StaffMe } | { ok: false; error: ApiError }> {
  return apiGet<StaffMe>("/api/staff/me");
}

export function fetchPresets(): Promise<
  { ok: true; data: Preset[] } | { ok: false; error: ApiError }
> {
  return apiGet<Preset[]>("/api/staff/points/presets");
}

export function staffLogin(
  email: string,
  password: string,
): Promise<{ ok: true } | { ok: false; error: ApiError }> {
  return apiPost<null>("/api/staff/auth/login", { email, password }).then((result) =>
    result.ok ? { ok: true } : { ok: false, error: result.error },
  );
}

export function staffLogout(): Promise<{ ok: true } | { ok: false; error: ApiError }> {
  return apiPost<null>("/api/staff/auth/logout", {}).then((result) =>
    result.ok ? { ok: true } : { ok: false, error: result.error },
  );
}
