// The admin list filters, defined ONCE. The list (AdminList) and the CSV export
// (ExportBar) both turn the same applied values into a request through these
// functions, so a filter is read the same way on screen and in the file.

export type ListKind = "attendees" | "points-log";
export type Values = Record<string, string>;

export type FilterDef = {
  key: string;
  label: string;
  type: "text" | "number" | "date" | "select";
  options?: [value: string, label: string][];
  // Dates: a range start is the beginning of the picked local day; a range end
  // is the beginning of the day AFTER the picked one, because the API's end is
  // exclusive while the picker reads as "up to and including this day".
  boundary?: "start" | "end";
};

export const FILTERS: Record<ListKind, FilterDef[]> = {
  attendees: [
    { key: "q", label: "Search name or e-mail", type: "text" },
    { key: "status", label: "Status", type: "select", options: [["ACTIVE", "Active"], ["PROVISIONAL", "Provisional"]] },
    { key: "sponsor_consent", label: "Sponsor consent", type: "select", options: [["true", "Given"], ["false", "Not given"]] },
    { key: "registered_from", label: "Registered from", type: "date", boundary: "start" },
    { key: "registered_to", label: "Registered until", type: "date", boundary: "end" },
  ],
  "points-log": [
    { key: "user_id", label: "Attendee ID", type: "number" },
    { key: "booth_id", label: "Booth ID", type: "number" },
    { key: "task_id", label: "Activity ID", type: "number" },
    { key: "staff_id", label: "Staff ID", type: "number" },
    { key: "source", label: "Source", type: "select", options: [["TASK", "Task award"], ["FEEDBACK", "Feedback reward"]] },
    { key: "entry_type", label: "Entry type", type: "select", options: [["AWARD", "Award entry"], ["REVERSAL", "Reversal entry"]] },
    { key: "created_from", label: "From date", type: "date", boundary: "start" },
    { key: "created_to", label: "Until date", type: "date", boundary: "end" },
  ],
};

const RANGE: Record<ListKind, [from: string, to: string]> = {
  attendees: ["registered_from", "registered_to"],
  "points-log": ["created_from", "created_to"],
};

export function instant(day: string, boundary: "start" | "end"): string {
  const date = new Date(`${day}T00:00:00`); // local midnight of the picked day
  if (boundary === "end") date.setDate(date.getDate() + 1);
  return date.toISOString();
}

function value(def: FilterDef, values: Values): string {
  return (values[def.key] ?? "").trim();
}

// The applied filters as URL query parameters (the list request).
export function listQuery(kind: ListKind, values: Values, cursor: string | null): string {
  const params = new URLSearchParams();
  for (const def of FILTERS[kind]) {
    const raw = value(def, values);
    if (!raw) continue;
    params.set(def.key, def.type === "date" ? instant(raw, def.boundary ?? "start") : raw);
  }
  if (cursor) params.set("cursor", cursor);
  const text = params.toString();
  return text ? `?${text}` : "";
}

// The same filters as a JSON body (the export request): identical values, typed.
// There is deliberately no cursor and no page size - an export is the whole set.
export function exportBody(kind: ListKind, values: Values): Record<string, string | number | boolean> {
  const body: Record<string, string | number | boolean> = {};
  for (const def of FILTERS[kind]) {
    const raw = value(def, values);
    if (!raw) continue;
    if (def.type === "date") body[def.key] = instant(raw, def.boundary ?? "start");
    else if (def.type === "number") body[def.key] = Number(raw);
    else if (def.key === "sponsor_consent") body[def.key] = raw === "true";
    else body[def.key] = raw;
  }
  return body;
}

// A message for a filter that cannot be sent, or "" when it can.
export function problem(kind: ListKind, values: Values): string {
  for (const def of FILTERS[kind]) {
    const raw = value(def, values);
    if (def.type === "number" && raw && !/^[1-9][0-9]{0,9}$/.test(raw)) return "Enter a whole number greater than zero.";
    if (def.type === "date" && raw && Number.isNaN(new Date(`${raw}T00:00:00`).getTime())) return "Enter a valid date.";
  }
  const [from, to] = RANGE[kind];
  const start = (values[from] ?? "").trim();
  const end = (values[to] ?? "").trim();
  if (start && end && start > end) return "The start date must not be after the end date.";
  return "";
}

export function sameFilters(kind: ListKind, a: Values, b: Values): boolean {
  return FILTERS[kind].every((def) => value(def, a) === value(def, b));
}
