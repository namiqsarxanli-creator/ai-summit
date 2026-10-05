// Reversing an AWARD in the points ledger (administrators only).
//
// The server decides everything that matters: the amount, the attendee, the
// booth and the activity all come from the original entry, so the request carries
// only the reason and an idempotency key. This file builds that request and the
// client-side checks that save a pointless round trip.

import { type ApiError, apiPost } from "./api";

export const REASON_MIN = 3;
export const REASON_MAX = 300;

// The ledger row as the admin list returns it (the fields this screen uses).
export type LedgerEntry = {
  id: number;
  user_id: number;
  attendee_name: string;
  booth_name: string | null;
  source: "TASK" | "FEEDBACK";
  entry_type: "AWARD" | "REVERSAL";
  points: number;
  activity: string;
  created_at: string;
  reversed_by_entry_id: number | null;
};

export type ReversalResult = {
  reversal_id: number;
  reversed_entry_id: number;
  points_reversed: number;
  resulting_total: number;
  created_at: string;
  replayed: boolean;
};

// Only an AWARD that nothing has reversed yet. (A REVERSAL cannot be reversed.)
export function canReverse(row: Record<string, unknown>): boolean {
  return row["entry_type"] === "AWARD" && row["reversed_by_entry_id"] === null && typeof row["id"] === "number";
}

// What the server will store: whitespace collapsed, like its own normalisation.
export function normalizeReason(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(" ");
}

// A message for a reason that cannot be sent, or "" when it can.
export function reasonProblem(text: string): string {
  const reason = normalizeReason(text);
  if (reason.length < REASON_MIN) return "Give a reason of at least 3 characters.";
  if (reason.length > REASON_MAX) return "The reason can be at most 300 characters.";
  if (/[\u0000-\u001f\u007f-\u009f]/.test(reason)) return "The reason may only contain readable characters.";
  return "";
}

export type ReverseResult = { ok: true; data: ReversalResult } | { ok: false; error: ApiError };

export function reverseEntry(entryId: number, reason: string, idempotencyKey: string): Promise<ReverseResult> {
  return apiPost<ReversalResult>(`/api/admin/points-log/${entryId}/reverse`, {
    reason,
    idempotency_key: idempotencyKey,
  });
}

// A fresh idempotency key. crypto.randomUUID needs a secure context; a plain-HTTP
// LAN address is not one, so fall back to a v4-shaped key from getRandomValues.
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  const bytes = c.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

// Every UI text of the reversal screen, so a test can check each one has an
// Azerbaijani and a Russian translation.
export const REVERSAL_TEXTS: string[] = [
  "Reverse…",
  "Reverse points",
  "Review this entry before you reverse it.",
  "Entry",
  "Attendee",
  "Booth",
  "Activity",
  "Points",
  "Date",
  "What happens",
  "The attendee's total goes down by the points of this entry.",
  "The original entry stays in the ledger. A new negative entry is added and linked to it, with your name and the reason.",
  "The leaderboard and the attendee's score update.",
  "This cannot be undone here: a reversal cannot itself be reversed. A mistaken reversal is corrected by awarding the points again.",
  "This does NOT make the activity available to this attendee again if it can be earned only once. Activities that allow repeats can still be earned again.",
  "This does NOT let this attendee earn the feedback reward at this booth again.",
  "Reason (required, 3–300 characters)",
  "Give a reason of at least 3 characters.",
  "The reason can be at most 300 characters.",
  "The reason may only contain readable characters.",
  "characters",
  "Reverse these points",
  "Reversing…",
  "Cancel",
  "Try again",
  "Back to the ledger",
  "Points reversed",
  "were taken away. The attendee's total is now",
  "Reversal entry",
  "This reversal had already been recorded; nothing was changed a second time.",
  "We could not tell whether the reversal was recorded. Trying again is safe: the same request can never reverse the entry twice.",
  "Feedback reward",
  "Task award",
];
