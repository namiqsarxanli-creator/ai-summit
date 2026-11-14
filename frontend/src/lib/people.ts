// People: compatible people, meeting requests, Match Corner meetings and the
// mutual-scan connections. The server decides scores, slots, tables and points.

import { apiGet, apiPost, type ApiResult } from "@/lib/api";
import { labelOf } from "@/lib/interestCard";

export type Person = {
  user_id: number;
  name: string;
  profession: string;
  score: number;
  shared_topics: string[];
  complement: [string, string] | null;
  shared_masterclasses: number;
  same_sector: boolean;
  reasons: ("complement" | "topics" | "masterclass" | "sector")[];
};

export type MeetingRequest = {
  id: number;
  status: "pending" | "accepted" | "declined" | "cancelled";
  direction: "incoming" | "outgoing";
  other: { user_id: number; name: string; profession: string };
  meeting: { table: string; starts_at: string; ends_at: string; prompts: string[] } | null;
};

export type ContactAction = { type: "LINKEDIN" | "EMAIL"; value: string };
// `points`: what THIS person holds from the connection (never its nominal value);
// `paid` / `status` say whether it was ever paid (backend people/connections.py).
export type ConnectionRow = {
  user_id: number; name: string; profession: string; kind: "match" | "other"; points: number;
  paid?: boolean; status?: string; contact: ContactAction;
};
export type ScanOutcome =
  | "waiting" | "awarded" | "already_paid" | "replaced" | "reversed" | "unpaid"
  | "same_company" | "ineligible" | "not_configured" | "closed" | "limit_reached";
export type ScanResult = {
  connected: boolean; mutual?: boolean; name: string; kind?: "match" | "other"; same_company?: boolean;
  points?: number; outcome?: ScanOutcome | string; reason?: string;
};

const NOT_PAID = "An organiser can review it.";

// Why a connection earned this person nothing (the same words for a new scan and
// for a connection that was never paid).
const ZERO_REASON: Record<string, string> = {
  same_company: "No points: you work at the same company.",
  ineligible: "No points: you are not part of the competition.",
  not_configured: "No points yet: the competition is not set up yet.",
  closed: "No points: the competition has closed.",
  limit_reached: "No points: your best four connections already count more.",
  not_paid: NOT_PAID,
};

// What a scan did for THIS person, in words: one-sided or mutual, the points
// actually added, and why nothing was added. `t` translates a fixed sentence.
export function scanMessage(r: ScanResult, t: (key: string) => string): string {
  const n = String(r.points ?? 0);
  const outcome = r.outcome ?? (r.connected ? "unpaid" : "waiting");
  if (!r.connected || outcome === "waiting") return t("Scanned. When they scan your QR too, you are connected.");
  switch (outcome) {
    case "awarded":
      return t("Connected. +{n} points for you. Their contact is below.").replace("{n}", n);
    case "already_paid":
      return t("Already connected. You hold {n} points from this connection.").replace("{n}", n);
    case "replaced":
      return t("Already connected. Its points were replaced by a better connection: only your best four count.");
    case "reversed":
      return t("Already connected. An organiser cancelled the points of this connection.");
    case "unpaid":
      return `${t("Already connected. This connection has not earned you points.")} ${t(ZERO_REASON[r.reason ?? "not_paid"] ?? NOT_PAID)}`;
    default:
      return `${t("Connected. Their contact is below.")} ${t(ZERO_REASON[outcome] ?? "Recorded, without points.")}`;
  }
}

export const fetchPeople = () => apiGet<{ people: Person[] }>("/api/attendees/me/people");
export const fetchMeetings = () => apiGet<{ timezone: string | null; requests: MeetingRequest[] }>("/api/attendees/me/meetings");
export const fetchConnections = () => apiGet<{ connections: ConnectionRow[] }>("/api/attendees/me/connections");
export const sendRequest = (recipientId: number, command: string) =>
  apiPost<MeetingRequest>("/api/attendees/me/meeting-requests", { recipient_id: recipientId, command_id: command });
export const respond = (id: number, accept: boolean) =>
  apiPost<MeetingRequest>(`/api/attendees/me/meeting-requests/${id}/respond`, { accept });
export const cancelRequest = (id: number) => apiPost<MeetingRequest>(`/api/attendees/me/meeting-requests/${id}/cancel`, {});
export const blockPerson = (userId: number) => apiPost<{ blocked: boolean }>("/api/attendees/me/blocks", { user_id: userId });
export const scanPerson = (cardToken: string) => apiPost<ScanResult>("/api/attendees/me/connections/scan", { card_token: cardToken });

export const REASON_TEXT: Record<Person["reasons"][number], string> = {
  complement: "One of you offers what the other is looking for.",
  topics: "You share interest topics.",
  masterclass: "You chose the same masterclass.",
  sector: "You work in the same field.",
};

// A discussion prompt key from the server -> [text, value to fill in].
export function promptText(key: string, translate: (text: string) => string): string {
  const [kind, id] = key.split(":");
  if (kind === "topic" && id) return translate("Share one experience with {topic}.").replace("{topic}", translate(labelOf("topics", id)));
  if (kind === "goal" && id) return translate("Talk about {goal}: what would help each of you?").replace("{goal}", translate(labelOf("seeking", id)));
  const generic: Record<string, string> = {
    intro: "Introduce yourselves: what brought you to AI Summit?",
    takeaway: "What is the most useful thing you heard today?",
    next_step: "Is there one next step you could take together?",
  };
  return translate(generic[key] ?? "Introduce yourselves: what brought you to AI Summit?");
}

const REASONS: Record<string, string> = {
  self: "You cannot do this with yourself.",
  already_open: "You already sent this person a request.",
  they_asked_you: "This person has already asked you. Answer their request below.",
  daily_limit: "You can send up to 10 meeting requests a day.",
  not_pending: "This request has already been answered.",
  decline_instead: "Decline the request instead.",
  no_tables: "No Match Corner table is set up yet. Try again later.",
  no_free_slot: "No Match Corner table is free today any more.",
  not_configured: "Meetings are not set up yet.",
  blocked: "You cannot connect with this person.",
  changed_meanwhile: "Something changed meanwhile. Refresh and try again.",
  command_conflict: "This action does not match the original request.",
};

export function peopleFailure(result: Extract<ApiResult<unknown>, { ok: false }>, messageFor: (code: string) => string): string {
  const reason = result.error.details.reason;
  if (typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  if (result.error.code === "PEOPLE_NOT_FOUND") return "This person is not available.";
  return messageFor(result.error.code);
}
