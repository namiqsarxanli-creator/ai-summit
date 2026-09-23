// The attendee's own private profile (ARCHITECTURE.md §8.1,
// IMPLEMENTATION_PLAN.md M08: "registration data, selected interests,
// consent state — private view only"). A separate endpoint and type
// from `lib/session.ts`'s `Attendee` — that one stays the deliberately
// minimal shape M05 defined for lightweight session-check widgets;
// this one is the fuller shape the one screen that shows the
// attendee's own record needs. Identity comes from the session cookie
// on every call — nothing here ever names or selects an attendee.

import { apiGet, type ApiResult } from "@/lib/api";

export type ProfileInterest = { slug: string; name: string };

export type AttendeeProfile = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  profession: string;
  linkedin_url: string | null;
  sponsor_consent: boolean;
  interests: ProfileInterest[];
};

export function fetchProfile(): Promise<ApiResult<AttendeeProfile>> {
  return apiGet<AttendeeProfile>("/api/attendees/me/profile");
}
