// The venue's big screen during the day (backend competition/screen.py). The
// server decides the state and ranks; this file only reads.

import { apiGet } from "@/lib/api";

export type LiveState = "not_configured" | "disabled" | "open" | "review" | "published";
export type LivePerson = { place: number; name: string; points: number; photo_url: string | null };
export type LiveAward = { name: string; points: number; category: string; at: string };
export type LiveScreen = {
  state: LiveState;
  missing: string[];
  server_time: string;
  timezone: string;
  closes_at: string | null;
  top: LivePerson[];
  recent: LiveAward[];
  photos: { photo_url: string }[];
};

export const fetchLiveScreen = () => apiGet<LiveScreen>("/api/screen/live");

// What `missing` names, for the "not set up yet" state.
// Where an administrator sets it: the section of the admin console (/admin?section=).
export const MISSING_SECTION: Record<string, string> = {
  event: "masterclasses",
  close: "competition",
  competition_mode: "competition",
};

export const MISSING_LABEL: Record<string, string> = {
  event: "The event and its date",
  close: "The competition close time (17:30)",
  competition_mode: "The competition ranking switched on",
};

// Milliseconds left until `closesAt` by the SERVER's clock: `offsetMs` is the
// server's time minus this device's time, measured when the data arrived.
export function remainingMs(closesAt: string, offsetMs: number, nowMs: number): number {
  return Math.max(0, Date.parse(closesAt) - (nowMs + offsetMs));
}

export function formatCountdown(ms: number): string {
  const total = Math.ceil(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const two = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}
