// AI Playground: 15-minute sessions at participating booths, three places each.
// Everything is decided by the server (seats, the booking window, the queue,
// invitation expiry); this module only carries the observed revision/version so
// a stale screen can never overwrite a newer state.

import { apiGet, apiPost, type ApiResult } from "@/lib/api";

export type PlaygroundStatus =
  | "confirmed"
  | "invited"
  | "waitlisted"
  | "checked_in"
  | "completed"
  | "cancelled"
  | "expired"
  | "no_show";

export type PlaygroundBooking = {
  id: number;
  slot_id: number;
  status: PlaygroundStatus;
  version: number;
  expires_at: string | null;
};

export type PlaygroundInvitation = PlaygroundBooking & { token: string };

export type PlaygroundSlot = {
  id: number;
  event_id: number;
  booth_id: number;
  booth: string;
  booth_description?: string | null;
  number: number;
  starts_at: string;
  ends_at: string;
  timezone: string;
  seats_left: number;
  bookable: boolean;
  queue_open: boolean;
  cancellable: boolean;
  mine: PlaygroundBooking | null;
  revision: string;
  invitations: PlaygroundInvitation[];
  queue_position: number | null;
};

export type PlaygroundProgram = { server_time: string; slots: PlaygroundSlot[] };

export type InvitationPreview = {
  slot_id: number;
  starts_at: string;
  ends_at: string;
  booth_id: number;
  expires_at: string;
};

export type Admission = PlaygroundBooking & { admitted: boolean; reason?: string };

export const SEATS_PER_SLOT = 3;
// Statuses that hold (or held) the attendee's own time in that slot.
export const HOLDING: readonly PlaygroundStatus[] = ["confirmed", "waitlisted", "checked_in", "completed"];
// After these the attendee may book or queue again.
export const RELEASED: readonly PlaygroundStatus[] = ["cancelled", "expired", "no_show"];

export const fetchPlayground = () => apiGet<PlaygroundProgram>("/api/playground");

export const bookPlayground = (slot: PlaygroundSlot, command: string, queue = false) =>
  apiPost<PlaygroundBooking>(`/api/playground/slots/${slot.id}/book`, {
    command_id: command,
    expected_revision: slot.revision,
    queue,
  });

export const cancelPlayground = (booking: PlaygroundBooking) =>
  apiPost<PlaygroundBooking>(`/api/playground/bookings/${booking.id}/cancel`, {
    expected_version: booking.version,
  });

export const invitePlayground = (slot: PlaygroundSlot, command: string) =>
  apiPost<PlaygroundInvitation>(`/api/playground/slots/${slot.id}/invite`, { command_id: command });

export const acceptPlayground = (token: string) =>
  apiPost<PlaygroundBooking>("/api/playground/invitations/accept", { token });

export const inspectPlayground = (token: string) =>
  apiPost<InvitationPreview>("/api/playground/invitations/inspect", { token });

export const admitPlayground = (booking: PlaygroundBooking) =>
  apiPost<Admission>(`/api/staff/playground/bookings/${booking.id}/admit`, {
    expected_version: booking.version,
  });

// The invitation travels in the URL FRAGMENT, which browsers never send to a
// server or put in a Referer header.
export const invitationLink = (origin: string, token: string) =>
  `${origin}/playground#invite=${encodeURIComponent(token)}`;

const REASONS: Record<string, string> = {
  active_booking_limit: "You can hold up to 3 active Playground bookings.",
  schedule_overlap: "This overlaps another booking.",
  masterclass_overlap: "This overlaps your masterclass choice or waitlist.",
  playground_overlap: "This overlaps your Playground booking.",
  mentor_overlap: "This overlaps your mentor group place.",
  slot_full: "This slot is full. You can join the on-site queue.",
  choice_changed: "Your booking changed. Review the refreshed programme.",
  booking_closed: "Advance booking is closed for this slot.",
  slot_closed: "This session has ended.",
  cancellation_closed: "Cancellation closes 15 minutes before the session.",
  invitation_expired: "This invitation has expired or was withdrawn.",
  already_booked: "You already have a place in this slot.",
  self_invitation: "Share this invitation with another registered attendee.",
  queue_order: "Another person is ahead in the queue.",
  outside_slot: "This session is not open yet or has ended.",
  join_queue_again: "Your reserved place expired. Join the on-site queue.",
  command_conflict: "This action does not match the original request.",
  confirmed_booking_required: "Reserve your own place before inviting teammates.",
};

export const FALLBACK = "The action could not be completed. Refresh and try again.";

export function playgroundError(reason: unknown): string {
  return REASONS[String(reason)] ?? FALLBACK;
}

// The text for any failed call: a Playground rule, or the shared error copy.
export function failureText(
  result: Extract<ApiResult<unknown>, { ok: false }>,
  messageFor: (code: string) => string,
): string {
  return result.error.code === "PLAYGROUND_CONFLICT"
    ? playgroundError(result.error.details.reason)
    : messageFor(result.error.code);
}

export const STATUS_LABEL: Record<PlaygroundStatus, string> = {
  confirmed: "Place reserved",
  invited: "Invitation hold",
  waitlisted: "On-site queue",
  checked_in: "Checked in",
  completed: "Completed",
  cancelled: "Cancelled",
  expired: "Expired",
  no_show: "Reserved place expired",
};

export const slotLabel = (slot: PlaygroundSlot) => `P${String(slot.number).padStart(2, "0")}`;
