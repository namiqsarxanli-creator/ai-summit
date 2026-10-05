// The admin audit view: the API's types, the filters and what is shown for them.
//
// Everything here is plain data and pure functions. The server has already
// decided which fields of an event may be seen (an allowlist per event kind,
// backend/app/modules/admin/audit.py); this file only decides how they read.
// Values are always rendered as TEXT by the component - never as markup.

import { instant } from "@/components/admin/listFilters";

export type AuditValue = string | number | boolean | null;

export type AuditResult = {
  kind: "attendees" | "points-log" | "sponsor-contacts" | null;
  outcome: "started" | "rejected" | "stream_finished" | "interrupted" | "failed" | null;
  reason: string | null;
  error_class: string | null;
  rows_expected: number | null;
  rows_written: number | null;
  rows_matched: number | null;
  max_rows: number | null;
  bytes_written: number | null;
  elapsed_ms: number | null;
  snapshot_at: string | null;
  filters_used: string[];
};

export type AuditEvent = {
  id: number;
  created_at: string;
  actor_id: number;
  actor_name: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  operation_id: string | null;
  before: Record<string, AuditValue> | null;
  after: Record<string, AuditValue> | null;
  changed_fields: string[];
  result: AuditResult | null;
  hidden_field_count: number;
  details: "known" | "limited";
};

export type AuditPageData = { items: AuditEvent[]; next_cursor: string | null; limit: number };

// --- filters ------------------------------------------------------------------

export type AuditValues = {
  actor_id?: string;
  action?: string;
  target_type?: string;
  target_id?: string;
  created_from?: string;
  created_to?: string;
};

export const NO_FILTERS: AuditValues = {};

// [value, label] - the label is an English UI text, translated by the component.
export const ACTION_OPTIONS: [string, string][] = [
  ["staff.create", "Staff account created"],
  ["staff.deactivate", "Staff account deactivated"],
  ["booth.create", "Booth created"],
  ["booth.update", "Booth updated"],
  ["booth.weight", "Booth interest weight set"],
  ["booth.weight.remove", "Booth interest weight removed"],
  ["interest.create", "Interest created"],
  ["interest.update", "Interest updated"],
  ["task.create", "Task created"],
  ["task.update", "Task updated"],
  ["preset.create", "Point preset created"],
  ["preset.update", "Point preset updated"],
  ["event_config.update", "Event settings updated"],
  ["points.reverse", "Points entry reversed"],
  ["staff.password_change", "Password changed"],
  ["staff.password_reset_requested", "Password reset email requested"],
  ["staff.password_reset", "Password reset completed"],
  ["export.started", "CSV export started"],
  ["export.finished", "CSV export finished"],
  ["export.rejected", "CSV export refused"],
];

export const TARGET_OPTIONS: [string, string][] = [
  ["booth", "Booth"],
  ["booth_interest", "Booth interest weight"],
  ["interest", "Interest"],
  ["task", "Task"],
  ["task_preset_point", "Point preset"],
  ["staff_account", "Staff account"],
  ["event_config", "Event settings"],
  ["points_log", "Points ledger entry"],
  ["export", "CSV export"],
];

// A label looked up by a code the SERVER sent: only the map's own entries count
// (a code such as "constructor" must not find an inherited property).
export function ownLabel(map: Record<string, string>, key: string): string | null {
  return Object.prototype.hasOwnProperty.call(map, key) ? (map[key] ?? null) : null;
}

const actionLabels = new Map(ACTION_OPTIONS);
const targetLabels = new Map(TARGET_OPTIONS);

// The label of a kind of event / target, or null when this page does not know
// it (a newer server): the caller then shows the raw code as plain text.
export function actionLabel(action: string): string | null {
  return actionLabels.get(action) ?? null;
}
export function targetLabel(targetType: string): string | null {
  return targetLabels.get(targetType) ?? null;
}

function text(values: AuditValues, key: keyof AuditValues): string {
  return (values[key] ?? "").trim();
}

// The applied filters as URL query parameters. Dates are timezone-aware instants:
// a start is the beginning of the picked local day, an end the beginning of the
// day AFTER the picked one (the API's end is exclusive, the picker reads as
// "up to and including").
export function auditQuery(values: AuditValues, cursor: string | null): string {
  const params = new URLSearchParams();
  for (const key of ["actor_id", "action", "target_type", "target_id"] as const) {
    const value = text(values, key);
    if (value) params.set(key, value);
  }
  const from = text(values, "created_from");
  const to = text(values, "created_to");
  if (from) params.set("created_from", instant(from, "start"));
  if (to) params.set("created_to", instant(to, "end"));
  if (cursor) params.set("cursor", cursor);
  const query = params.toString();
  return query ? `?${query}` : "";
}

// A message for a filter that cannot be sent, or "" when it can.
export function auditProblem(values: AuditValues): string {
  const actor = text(values, "actor_id");
  if (actor && !/^[1-9][0-9]{0,9}$/.test(actor)) return "Enter a whole number greater than zero.";
  const target = text(values, "target_id");
  if (target && !text(values, "target_type")) return "Choose a target type to search by target ID.";
  if (target && !/^[A-Za-z0-9:_.-]{1,64}$/.test(target)) return "The target ID may only contain letters, digits and : _ . -";
  for (const key of ["created_from", "created_to"] as const) {
    const day = text(values, key);
    if (day && Number.isNaN(new Date(`${day}T00:00:00`).getTime())) return "Enter a valid date.";
  }
  const from = text(values, "created_from");
  const to = text(values, "created_to");
  if (from && to && from > to) return "The start date must not be after the end date.";
  return "";
}

export function sameAuditFilters(a: AuditValues, b: AuditValues): boolean {
  return (["actor_id", "action", "target_type", "target_id", "created_from", "created_to"] as const)
    .every((key) => text(a, key) === text(b, key));
}

export function anyAuditFilter(values: AuditValues): boolean {
  return !sameAuditFilters(values, NO_FILTERS);
}

// --- what an event shows --------------------------------------------------------

export type DiffRow = { field: string; before: AuditValue | undefined; after: AuditValue | undefined; changed: boolean };

// One row per field either side recorded, in a stable order (id first), with the
// server's own verdict on what changed.
export function diffRows(event: AuditEvent): DiffRow[] {
  const before = event.before ?? {};
  const after = event.after ?? {};
  const changed = new Set(event.changed_fields);
  const names = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  names.sort((a, b) => (a === "id" ? -1 : b === "id" ? 1 : 0)); // stable for the rest
  return names.map((field) => ({
    field,
    before: field in before ? before[field] : undefined,
    after: field in after ? after[field] : undefined,
    changed: changed.has(field),
  }));
}

export const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  slug: "Slug",
  name: "Name",
  title: "Title",
  label: "Label",
  description: "Description",
  location_label: "Location",
  role: "Role",
  booth_id: "Booth ID",
  interest_id: "Interest ID",
  is_active: "Active",
  allow_repeat_task_points: "Repeat points allowed (booth)",
  allow_repeat_points: "Repeat points allowed",
  allow_repeat_task_points_default: "Repeat task points by default",
  sort_order: "Sort order",
  points: "Points",
  weight: "Weight",
  removed: "Removed",
  feedback_points: "Feedback reward points",
  leaderboard_top_n: "Leaderboard top N",
  points_log_id: "Ledger entry ID",
  reversal_id: "Reversal entry ID",
  user_id: "Attendee ID",
  task_id: "Activity ID",
  source: "Source",
  reversed: "Reversed",
  reason: "Reason",
  sessions_revoked: "Sessions ended",
  initiated_by: "Started by",
};

// Words some fields hold (fixed sets), as UI texts.
export const VALUE_LABELS: Record<string, Record<string, string>> = {
  role: { ADMIN: "Administrator", BOOTH_STAFF: "Booth staff" },
  source: { TASK: "Task award", FEEDBACK: "Feedback reward" },
  initiated_by: { self: "The staff member", admin: "An administrator" },
};

export const EXPORT_KIND_LABELS: Record<string, string> = {
  attendees: "Attendees",
  "points-log": "Points ledger",
  "sponsor-contacts": "Sponsor contacts",
};

export const OUTCOME_LABELS: Record<string, string> = {
  started: "Started",
  rejected: "Refused before it started",
  stream_finished: "The server sent the whole file",
  interrupted: "Interrupted before the end",
  failed: "Failed while sending",
};

export const REASON_LABELS: Record<string, string> = {
  row_limit: "More rows than the export limit",
  time_limit: "Ran past the time limit",
  closed_before_end: "The download was closed before the end",
  error: "A server error",
  row_count_mismatch: "The number of rows changed during the export",
  stalled: "The download stopped being read",
};

export const FILTER_NAME_LABELS: Record<string, string> = {
  q: "Search",
  status: "Status",
  sponsor_consent: "Sponsor consent",
  registered_from: "Registered from",
  registered_to: "Registered until",
  user_id: "Attendee ID",
  booth_id: "Booth ID",
  task_id: "Activity ID",
  staff_id: "Staff ID",
  source: "Source",
  entry_type: "Entry type",
  created_from: "From date",
  created_to: "Until date",
};

// Every UI text of the audit section, in one place so a test can check each one
// has an Azerbaijani and a Russian translation.
export const AUDIT_TEXTS: string[] = [
  "Audit log",
  "Audit log filters",
  "Read-only trail of administrator changes and CSV exports. Viewing it records nothing.",
  "Times are shown in your local time zone",
  "Staff ID",
  "Action",
  "Target type",
  "Target ID",
  "Choose a target type to search by target ID.",
  "The target ID may only contain letters, digits and : _ . -",
  "Loading audit events…",
  "No audit events match these filters.",
  "No audit events recorded yet.",
  "Audit events",
  "Details",
  "Hide details",
  "Actor",
  "Target",
  "Result",
  "Before",
  "After",
  "Field",
  "Changed",
  "Not recorded",
  "Event kind",
  "Operation ID",
  "Show the events of this export",
  "This export's events are shown.",
  "Some fields are hidden because they are personal or not needed here",
  "This kind of event is not recognised by this page, so only who, when and what it concerned are shown.",
  "Outcome",
  "Reason",
  "Error type",
  "Rows expected",
  "Rows sent",
  "Rows matched",
  "Row limit",
  "Bytes sent",
  "Duration (ms)",
  "Data snapshot taken",
  "Filters used",
  "no filters",
  "The server records what it sent, not whether the browser saved the file.",
  "Yes",
  "No",
  "Started",
  "Refused before it started",
  "The server sent the whole file",
  "Interrupted before the end",
  "Failed while sending",
  "More rows than the export limit",
  "Ran past the time limit",
  "The download was closed before the end",
  "A server error",
  "The number of rows changed during the export",
  "The download stopped being read",
  "Administrator",
  "Booth staff",
  "The staff member",
  "An administrator",
  "Task award",
  "Feedback reward",
  ...ACTION_OPTIONS.map(([, label]) => label),
  ...TARGET_OPTIONS.map(([, label]) => label),
  ...Object.values(FIELD_LABELS),
  ...Object.values(FILTER_NAME_LABELS),
  ...Object.values(EXPORT_KIND_LABELS),
];
