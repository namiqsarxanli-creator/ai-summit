// Staff session state (ARCHITECTURE.md §12, IMPLEMENTATION_PLAN.md
// M09). A DIFFERENT principal from the attendee session in
// `lib/session.ts` — this code never reads or writes the attendee
// endpoints, and vice versa (§12.2, §24.3: booth authority is derived
// server-side from THIS session, never supplied by the client).

import { type ApiError, apiGet, apiPost } from "./api";

// VOLUNTEER: no booth, no powers of its own - only scopes an administrator grants.
export type StaffRole = "BOOTH_STAFF" | "ADMIN" | "VOLUNTEER";

export type StaffMe = {
  // The account's own numbers, as the points list shows them.
  staff_id?: number;
  booth_id?: number | null;
  full_name: string;
  role: StaffRole;
  // `null` for ADMIN — an admin has no booth (§7.7's CHECK constraint).
  booth_name: string | null;
  // What this phone files offline scans under (lib/offlineQueue.ts).
  queue_owner: string;
  dataset_generation: number;
};

export type Preset = { points: number; label: string | null };

// One awardable activity of the signed-in staff member's own booth. `points`
// is what the server will award for it (display only - the award request
// cannot carry a value); `repeatable` says whether one attendee may earn it
// more than once.
export type StaffTask = {
  id: number;
  title: string;
  description: string | null;
  points: number;
  repeatable: boolean;
  // Opaque server-issued revision of this rule; echoed back on the award.
  rule_revision: string;
};

export function fetchStaffTasks(): Promise<
  { ok: true; data: StaffTask[] } | { ok: false; error: ApiError }
> {
  return apiGet<StaffTask[]>("/api/staff/tasks");
}

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
