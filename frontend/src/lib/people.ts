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
export type ConnectionRow = { user_id: number; name: string; profession: string; kind: "match" | "other"; points: number; contact: ContactAction };
export type ScanResult = { connected: boolean; name: string; kind?: "match" | "other"; same_company?: boolean; points?: number; outcome?: string };

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
