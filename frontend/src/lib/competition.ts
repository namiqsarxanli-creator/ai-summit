// Beyond Now competition: verified participation only. The server decides every
// window, scope and award; these calls carry no time and no points.

import { apiGet, apiPost, type ApiResult } from "@/lib/api";
import { tokenFromScannedText } from "@/lib/scanner";

export type AwardOutcome = "awarded" | "already" | "ineligible" | "not_configured" | "closed" | "category_full" | "not_scored";
export type Award = { outcome: AwardOutcome; points: number };

export type PanelScanResult = {
  panel_id: number;
  phase: "entry" | "exit";
  recorded: ("entry" | "exit")[];
  award: Award | null;
  grand_final?: boolean;
};

export type CategoryTotal = { category: string; points: number; maximum: number };
export type MyCompetition = {
  eligible: boolean;
  total: number;
  maximum: number;
  categories: CategoryTotal[];
  // Beyond Now Founder: the four zones, the bonus and the Lucky Draw eligibility.
  zones?: Record<string, boolean>;
  founder?: boolean;
  founder_rule_confirmed?: boolean;
  lucky_draw?: boolean;
  final?: { version: number; place: number | null; points: number | null; founder: boolean } | null;
  final_status?: "not_configured" | "disabled" | "open" | "pending_review" | "published";
};
// kind "grand_final": the Grand Final check-in (one code, no points of its own;
// it completes Beyond Now Founder) - never a Qmeter target.
export type PublicPanel = { id: number; title: string; kind?: "panel" | "grand_final"; starts_at: string; ends_at: string; timezone: string };
export type Scope = { scope: "MASTERCLASS_DOOR" | "GAME_MASTER" | "MENTOR_HOST"; target_id: number | null };

export type DoorResult = { phase: "entry" | "exit"; name: string; standing: boolean; already?: boolean; award?: Award };
export type CompleteResult = { name: string; award: Award };
export type Identified = { user_id: number; name: string; phone: string };

// What a panel's screen shows: P<id>.<ENTRY|EXIT>.<version>.<signature>.
const PANEL_CODE = /^P[1-9][0-9]*\.(ENTRY|EXIT)\.[1-9][0-9]*\.[0-9a-f]{64}$/;
// The six digits shown under the QR, typed by hand ("482 913" or "482913").
const SHORT_CODE = /^[0-9]{6}$/;
export const isPanelCode = (text: string) => PANEL_CODE.test(text.trim()) || SHORT_CODE.test(text.replace(/[\s-]/g, ""));

// The screen's QR is a link to My points carrying the code (7 Nov 2026): a phone's own
// camera opens the site instead of searching the bare code as text. The app's scanner
// reads the same link, so both ways end in the same code.
export function panelScanUrl(code: string, origin: string): string {
  return `${origin}/points?code=${encodeURIComponent(code)}`;
}
export function panelCodeFrom(text: string): string {
  const raw = text.trim();
  if (!/^https?:\/\//i.test(raw)) return raw;
  try {
    const url = new URL(raw);
    return url.pathname === "/points" ? (url.searchParams.get("code") ?? "").trim() : raw;
  } catch {
    return raw;
  }
}

export const scanPanel = (code: string) =>
  apiPost<PanelScanResult>("/api/attendees/me/panels/scan", { code: code.trim() });
export const fetchMyCompetition = () => apiGet<MyCompetition>("/api/attendees/me/competition");
export const fetchPanels = () => apiGet<PublicPanel[]>("/api/panels");
export const fetchMyScopes = () => apiGet<{ scopes: Scope[] }>("/api/staff/me/scopes");

export const doorScan = (masterclassId: number, cardToken: string, phase: "ENTRY" | "EXIT") =>
  apiPost<DoorResult>(`/api/staff/masterclasses/${masterclassId}/door`, { card_token: cardToken, phase });
export const completePlayground = (slotId: number, cardToken: string) =>
  apiPost<CompleteResult>(`/api/staff/playground/slots/${slotId}/complete`, { card_token: cardToken });
export const identifyAttendee = (fullName: string, phone: string) =>
  apiPost<Identified>("/api/staff/qmeter/identify", { full_name: fullName, phone });
export const awardQmeter = (userId: number, panelId: number, reason: string) =>
  apiPost<Award>("/api/staff/qmeter/award", { user_id: userId, panel_id: panelId, reason });

// The attendee card token in what the camera read (a card URL or a bare token).
export const cardTokenFrom = (text: string) => tokenFromScannedText(text);

// The six characters under an attendee's QR (no 0/O, 1/I/L), typed when the
// camera cannot read the code; the server turns them into the card token.
const CARD_SHORT = /^[2-9A-HJKMNP-Z]{6}$/;
export const cardShortCode = (text: string) => {
  const code = text.replace(/[\s-]/g, "").toUpperCase();
  return CARD_SHORT.test(code) ? code : null;
};

export type CardText = { token: string } | { error: string };

/** A scanned card URL, or a typed short code looked up on the server. */
export async function resolveCardText(text: string, who: "staff" | "attendee"): Promise<CardText> {
  const token = cardTokenFrom(text);
  if (token) return { token };
  const code = cardShortCode(text);
  if (!code) return { error: "This is not an attendee QR code." };
  const path = who === "staff" ? "/api/staff/cards/lookup" : "/api/attendees/me/cards/lookup";
  const r = await apiPost<{ card_token: string }>(path, { code });
  if (r.ok) return { token: r.data.card_token };
  if (r.error.code === "NETWORK_ERROR" || r.status === 0) return { error: "No connection: a typed code needs the internet. Scan the QR instead." };
  return { error: r.error.code === "INVALID_QR" ? "No attendee has this code. Check the six characters." : "This is not an attendee QR code." };
}

export const CATEGORY_LABEL: Record<string, string> = {
  PROFILE: "Profile and interest card",
  PANEL: "Main Stage panels",
  QMETER: "Questions in Qmeter",
  MASTERCLASS: "Masterclasses",
  PLAYGROUND: "AI Playground",
  CONNECTION: "Connections",
  MENTOR: "Meet the Mentor",
  FOUNDER: "Beyond Now Founder",
};

export const OUTCOME_TEXT: Record<AwardOutcome, string> = {
  awarded: "Points added.",
  already: "Already counted. Nothing was added twice.",
  ineligible: "This person is not part of the competition.",
  not_configured: "The competition is not set up yet: no points can be earned yet.",
  closed: "The competition has closed. No more points can be earned.",
  category_full: "This category is already at its maximum.",
  not_scored: "Recorded, without points.",
};

const REASONS: Record<string, string> = {
  invalid_code: "This code is not valid. Scan the code on the main screen again.",
  outside_window: "This scan is outside its time window.",
  unknown_panel: "Unknown panel.",
  panel_not_started: "This panel has not started yet.",
  panel_not_ended: "Qmeter points are recorded after the panel ends.",
  unknown_masterclass: "Unknown masterclass.",
  booked_elsewhere: "This person holds a place in another masterclass of this block.",
  seats_held_until_plus_five: "Seats are held for their reservations until 5 minutes after the start.",
  full: "No seat or standing place is left.",
  no_entry: "No entry scan for this person.",
  unknown_slot: "Unknown session.",
  not_admitted: "This person's arrival is not confirmed for this session. Confirm it first in Playground arrivals and queue.",
};

export function scanFailure(result: Extract<ApiResult<unknown>, { ok: false }>, messageFor: (code: string) => string): string {
  const reason = result.error.details.reason;
  if (typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  if (result.error.code === "ATTENDEE_NOT_IDENTIFIED") return "No single attendee matches that name and phone.";
  return messageFor(result.error.code);
}
