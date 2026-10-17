// Meet the Mentor: configured 20-minute groups. The server decides seats, the
// waiting list, time clashes and points; these calls carry no time and no points.

import { apiGet, apiPost, type ApiResult } from "@/lib/api";
import type { Award } from "@/lib/competition";

export type MentorBooking = {
  id: number;
  session_id: number;
  status: "confirmed" | "waitlisted" | "attended";
  version: number;
  position: number | null;
};

export type MentorSession = {
  id: number;
  title: string;
  mentor_name: string | null;
  starts_at: string;
  ends_at: string;
  capacity: number;
  seats_left: number;
  waitlist_left: number;
  bookable: boolean;
  cancellable: boolean;
  revision: string;
  booking: MentorBooking | null;
};

export type MentorProgramme = { timezone: string; sessions: MentorSession[] };
export type HostSession = { id: number; title: string; mentor_name: string | null; starts_at: string; ends_at: string };
export type HostGroup = { session_id: number; capacity: number; people: { name: string; status: string }[] };

export const fetchMentorSessions = () => apiGet<MentorProgramme>("/api/mentor-sessions");
export const bookMentor = (id: number, revision: string, command: string) =>
  apiPost<{ id: number; status: string; version: number }>(`/api/mentor-sessions/${id}/book`, {
    command_id: command,
    expected_revision: revision,
  });
export const cancelMentor = (booking: MentorBooking) =>
  apiPost<{ id: number; status: string }>(`/api/mentor-bookings/${booking.id}/cancel`, {
    expected_version: booking.version,
  });

export const fetchHostSessions = () => apiGet<{ timezone: string; sessions: HostSession[] }>("/api/staff/mentor-sessions");
export const fetchHostGroup = (id: number) => apiGet<HostGroup>(`/api/staff/mentor-sessions/${id}`);
export const completeMentor = (id: number, cardToken: string) =>
  apiPost<{ name: string; award: Award }>(`/api/staff/mentor-sessions/${id}/complete`, { card_token: cardToken });

export const STATUS_TEXT: Record<MentorBooking["status"], string> = {
  confirmed: "Your place is confirmed.",
  waitlisted: "You are on the waiting list.",
  attended: "The host confirmed your participation.",
};

const REASONS: Record<string, string> = {
  session_full: "This group and its waiting list are full.",
  one_active_booking: "You already hold a mentor group place. Cancel it to choose another one.",
  booking_closed: "This session has started; booking is closed.",
  cancellation_closed: "This session has started; it can no longer be cancelled.",
  masterclass_overlap: "This time overlaps your masterclass choice.",
  playground_overlap: "This time overlaps your AI Playground booking.",
  choice_changed: "Something changed meanwhile. Refresh and try again.",
  command_conflict: "This action does not match the original request.",
  not_booked: "This person has no place in this group.",
  group_full: "The group is full: a waiting person cannot be added.",
  outside_window: "This scan is outside its time window.",
};

export function mentorFailure(result: Extract<ApiResult<unknown>, { ok: false }>, messageFor: (code: string) => string): string {
  const reason = result.error.details.reason;
  if (typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  if (result.error.code === "MENTOR_NOT_FOUND") return "This mentor session is not available.";
  return messageFor(result.error.code);
}
