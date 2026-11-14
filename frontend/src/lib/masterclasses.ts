// The Beyond Now masterclass programme and the attendee's own reservations.
// Everything shown comes from the server: dates, rooms, capacities, the time
// zone and whether a block is still open for changes (`changes_open`, decided
// by the SERVER clock). Nothing here trusts the phone's clock.

import { apiGet, apiRequest, type ApiResult } from "@/lib/api";
import { isLanguage, translate } from "@/lib/i18n";

export type Availability = "available" | "waitlist" | "full";

export type MasterclassItem = {
  id: number;
  title: string;
  description: string | null;
  speaker: string | null;
  room: string | null;
  room_display_number?: number | null;
  capacity: number;
  waitlist_capacity: number;
  confirmed_count: number;
  waitlist_count: number;
  seats_left: number;
  availability: Availability;
};

export type Block = {
  display_number?: number | null;
  id: number;
  code: string;
  title: string;
  starts_at: string;
  ends_at: string;
  change_deadline: string;
  changes_open: boolean;
  masterclasses: MasterclassItem[];
};

export type ProgramEvent = {
  code: string;
  title: string;
  venue: string | null;
  timezone: string;
  event_date: string;
  blocks: Block[];
  unscheduled: { id: number; title: string; description: string | null; speaker: string | null }[];
};

export type Program = { server_time: string; events: ProgramEvent[] };

export type Choice = {
  block_id: number;
  masterclass_id: number;
  status: "confirmed" | "waitlisted";
  waitlist_position: number | null;
  change_deadline: string;
  changes_open: boolean;
};

export type MyChoices = { server_time: string; choices: Choice[]; revisions: Record<string, string> };

export type ChoiceResult = {
  outcome: "confirmed" | "waitlisted" | "unchanged" | "cancelled";
  choices: MyChoices;
};

export function fetchProgram(): Promise<ApiResult<Program>> {
  return apiGet<Program>("/api/masterclasses");
}

export function fetchMyChoices(): Promise<ApiResult<MyChoices>> {
  return apiGet<MyChoices>("/api/attendees/me/masterclasses");
}

// PUT: sending the same choice again changes nothing on the server, so a retry
// can never create a second reservation.
export function chooseMasterclass(
  blockId: number,
  masterclassId: number,
  flags: { joinWaitlist?: boolean; giveUpConfirmedSeat?: boolean; expectedRevision?: string } = {},
): Promise<ApiResult<ChoiceResult>> {
  return apiRequest<ChoiceResult>(`/api/attendees/me/masterclasses/${blockId}`, "PUT", {
    masterclass_id: masterclassId,
    expected_revision: flags.expectedRevision ?? "0",
    join_waitlist: flags.joinWaitlist ?? false,
    give_up_confirmed_seat: flags.giveUpConfirmedSeat ?? false,
  });
}

export function cancelChoice(blockId: number, expectedRevision = "0"): Promise<ApiResult<ChoiceResult>> {
  return apiRequest<ChoiceResult>(`/api/attendees/me/masterclasses/${blockId}`, "DELETE", { expected_revision: expectedRevision });
}

// A time of the event, in the EVENT's time zone (from the server), never the
// phone's. Always 24-hour, as the programme is printed.
export function eventTime(iso: string, timeZone: string, language: string): string {
  return new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  }).format(new Date(iso));
}

// Browsers ship no Azerbaijani month names (Chrome prints "2026 M11 13"), so
// Azerbaijani dates are written out here.
const AZ_MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avqust", "sentyabr", "oktyabr", "noyabr", "dekabr"];

export function eventDate(isoDate: string, language: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  if (language === "az" && year && month && day) return `${day} ${AZ_MONTHS[month - 1]} ${year}`;
  // A calendar date (no time): formatted in UTC so it never shifts a day.
  return new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

export type Action =
  | { kind: "choose" }
  | { kind: "waitlist" }
  | { kind: "change" }
  | { kind: "change-to-waitlist"; givesUpSeat: boolean }
  | { kind: "cancel" }
  | { kind: "none"; reason: "closed" | "full" };

// What the attendee can do with one masterclass, given their choice in its block.
export function actionFor(block: Block, item: MasterclassItem, mine: Choice | undefined): Action {
  if (!block.changes_open) return { kind: "none", reason: "closed" };
  if (mine?.masterclass_id === item.id) return { kind: "cancel" };
  if (item.availability === "full") return { kind: "none", reason: "full" };
  if (!mine) return item.availability === "available" ? { kind: "choose" } : { kind: "waitlist" };
  if (item.availability === "available") return { kind: "change" };
  return { kind: "change-to-waitlist", givesUpSeat: mine.status === "confirmed" };
}

// Explicit standard-label metadata, not a translation guessed from a free-form name.
export function programLabel(name: string, number: number | null | undefined, kind: "block" | "room", language: string): string {
  // A named room (e.g. "Ağdam Room") in the visitor's language when known.
  if (number == null) return isLanguage(language) ? translate(name, language) : name;
  const labels = kind === "block" ? { az: "Blok", en: "Block", ru: "Блок" } : { az: "Zal", en: "Hall", ru: "Зал" };
  return `${labels[language as keyof typeof labels] ?? labels.en} ${number}`;
}
