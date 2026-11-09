// The attendee's notifications (GET /api/attendees/me/notifications): what the
// reminder emails say, and what other people did that waits for an answer. The
// server derives every item from the real rows; this file only words them. Which
// ones are new is this device's own business: the time the list was last opened.

import { apiGet, type ApiResult } from "@/lib/api";
import { CATEGORY_LABEL } from "@/lib/competition";
import { eventTime } from "@/lib/masterclasses";

export type NotificationKind =
  | "programme_eve" | "programme_soon" | "grand_final_open" | "final_published"
  | "scan_back" | "connected" | "meeting_request" | "meeting_answer"
  | "pair_photo" | "pair_photo_accepted" | "points";

export type Notification = {
  id: string;
  kind: NotificationKind | string;
  at: string;
  href: string;
  params: Record<string, string | number | boolean | null>;
};

export type NotificationFeed = { server_time: string; timezone: string; items: Notification[] };

export const fetchNotifications = (): Promise<ApiResult<NotificationFeed>> =>
  apiGet<NotificationFeed>("/api/attendees/me/notifications");

const SEEN_KEY = "fm-notifications-seen";

export function lastSeen(): number {
  try {
    const v = Number(window.localStorage.getItem(SEEN_KEY));
    return Number.isFinite(v) ? v : 0;
  } catch {
    return 0;
  }
}

export function markSeen(at: number): void {
  try {
    window.localStorage.setItem(SEEN_KEY, String(at));
  } catch {
    // private mode: every item simply stays "new" on this device
  }
}

export function unreadCount(items: Notification[], seen: number): number {
  return items.filter((i) => Date.parse(i.at) > seen).length;
}

const ITEM_LABEL: Record<string, string> = {
  masterclass: "Masterclass",
  playground: "AI Playground",
  meeting: "Meeting",
  mentor: "Meet the Mentor",
};

function fill(text: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce((out, [key, value]) => out.split(`{${key}}`).join(String(value)), text);
}

/** The sentence for one item, already translated; `t` is the page's translator. */
export function describe(
  item: Notification,
  t: (key: string) => string,
  timezone: string,
  language: string,
): { title: string; detail: string } {
  const p = item.params;
  const name = String(p.name ?? "");
  switch (item.kind) {
    case "programme_eve":
      return { title: t("Tomorrow is AI Summit"), detail: fill(t("You have {count} sessions in My programme. Check the times and places."), { count: Number(p.count ?? 0) }) };
    case "programme_soon": {
      const at = typeof p.starts_at === "string" ? eventTime(p.starts_at, timezone, language) : "";
      const place = p.place ? ` · ${String(p.place)}` : "";
      return { title: fill(t("Starts at {time}"), { time: at }), detail: `${t(ITEM_LABEL[String(p.item)] ?? "Session")} · ${String(p.title ?? "")}${place}` };
    }
    case "grand_final_open": {
      const until = typeof p.ends_at === "string" ? eventTime(p.ends_at, timezone, language) : "";
      return { title: t("The Grand Final code is open"), detail: fill(t("Scan the code on the screen until {time}. Open My points."), { time: until }) };
    }
    case "final_published":
      return { title: t("The final results are out"), detail: t("See your place in My points.") };
    case "scan_back":
      return { title: fill(t("{name} scanned your QR"), { name }), detail: t("Scan their QR too, and you are connected.") };
    case "connected":
      return { title: fill(t("You and {name} are connected"), { name }), detail: t("You can now see each other's contact in People.") };
    case "meeting_request":
      return { title: fill(t("{name} wants to meet you"), { name }), detail: t("Answer the request in People.") };
    case "meeting_answer":
      return p.accepted
        ? { title: fill(t("{name} accepted your meeting request"), { name }), detail: t("See the time and table in People.") }
        : { title: fill(t("{name} declined your meeting request"), { name }), detail: t("Find other people to meet in People.") };
    case "pair_photo":
      return { title: fill(t("{name} shared a photo of you two"), { name }), detail: t("Accept or decline it in People.") };
    case "pair_photo_accepted":
      return { title: fill(t("{name} accepted your photo"), { name }), detail: t("It is now in both your reports.") };
    case "points": {
      const points = Number(p.points ?? 0);
      const label = t(CATEGORY_LABEL[String(p.source)] ?? "Points");
      return p.reversal
        ? { title: fill(t("{points} points taken back"), { points: Math.abs(points) }), detail: label }
        : { title: fill(t("+{points} points"), { points }), detail: label };
    }
    default:
      return { title: t("Notification"), detail: "" };
  }
}
