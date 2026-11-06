// The attendee's own private profile (ARCHITECTURE.md §8.1,
// IMPLEMENTATION_PLAN.md M08: "registration data, selected interests,
// consent state — private view only") and the ways they change it: details and
// interests, sponsor consent, profile photo. A separate endpoint and type from
// `lib/session.ts`'s `Attendee` — that one stays the deliberately minimal shape
// M05 defined for lightweight session-check widgets. Identity comes from the
// session cookie on every call — nothing here ever names or selects an attendee,
// and there is no e-mail field to change.

import { apiGet, apiPutBinary, apiRequest, type ApiResult } from "@/lib/api";
import { photoUrlsFrom, type PhotoUrls } from "@/lib/avatar";
import { companyAndCardErrors, isLinkedInUrl } from "@/components/registration/validation";
import {
  cardFrom,
  CARD_GROUPS,
  labelOf,
  sameCard,
  selectedIn,
  type InterestCard,
} from "@/lib/interestCard";

export type ProfileInterest = { slug: string; name: string };

export type AttendeeProfile = {
  // The attendee's own number, as the Game Master's points list shows it.
  attendee_id?: number;
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  profession: string;
  linkedin_url: string | null;
  sponsor_consent: boolean;
  // When, and under which wording, the consent was given; null while withheld.
  sponsor_consent_at: string | null;
  sponsor_consent_version: string | null;
  interests: ProfileInterest[];
  // Private to the attendee: never on the public card, leaderboard or exports.
  company: string | null;
  interest_card: InterestCard;
  // What an edit must send back as `expected_version` (optimistic concurrency).
  version: number;
  photo: PhotoUrls | null;
};

// What the server sent, made safe to render: a photo URL that is not one the
// server makes becomes "no photo", and a field an older server did not send
// gets its neutral value.
export function normalizeProfile(raw: AttendeeProfile): AttendeeProfile {
  return {
    ...raw,
    sponsor_consent_at: raw.sponsor_consent_at ?? null,
    sponsor_consent_version: raw.sponsor_consent_version ?? null,
    company: typeof raw.company === "string" ? raw.company : null,
    interest_card: cardFrom(raw.interest_card),
    version: typeof raw.version === "number" ? raw.version : 1,
    photo: photoUrlsFrom(raw.photo),
  };
}

// The body of a PROFILE_CONFLICT carries the profile as it stands now. It is
// data from the network: use it only if it has the shape of a profile.
export function parseProfile(value: unknown): AttendeeProfile | null {
  if (typeof value !== "object" || value === null) return null;
  const p = value as Record<string, unknown>;
  const text = (key: string) => typeof p[key] === "string";
  if (
    !text("first_name") || !text("last_name") || !text("phone") || !text("profession") ||
    !text("email") || typeof p.version !== "number" || !Array.isArray(p.interests) ||
    typeof p.sponsor_consent !== "boolean"
  ) {
    return null;
  }
  return normalizeProfile(value as AttendeeProfile);
}

export async function fetchProfile(): Promise<ApiResult<AttendeeProfile>> {
  const result = await apiGet<AttendeeProfile>("/api/attendees/me/profile");
  return result.ok ? { ok: true, data: normalizeProfile(result.data) } : result;
}

// --- editing details and interests --------------------------------------------

export type ProfileDraft = {
  first_name: string;
  last_name: string;
  phone: string;
  profession: string;
  linkedin_url: string;
  interest_slugs: string[];
  company: string;
  interest_card: InterestCard;
};

export function draftOf(profile: AttendeeProfile): ProfileDraft {
  return {
    first_name: profile.first_name,
    last_name: profile.last_name,
    phone: profile.phone,
    profession: profile.profession,
    linkedin_url: profile.linkedin_url ?? "",
    interest_slugs: profile.interests.map((interest) => interest.slug),
    company: profile.company ?? "",
    interest_card: profile.interest_card,
  };
}

// What would actually be sent: text trimmed (the server strips it the same way),
// interests as a set (order is not a change).
export function normalizedDraft(draft: ProfileDraft): ProfileDraft {
  return {
    first_name: draft.first_name.trim(),
    last_name: draft.last_name.trim(),
    phone: draft.phone.trim(),
    profession: draft.profession.trim(),
    linkedin_url: draft.linkedin_url.trim(),
    interest_slugs: [...new Set(draft.interest_slugs)].sort(),
    company: draft.company.trim(),
    interest_card: draft.interest_card,
  };
}

export function sameDraft(a: ProfileDraft, b: ProfileDraft): boolean {
  const x = normalizedDraft(a);
  const y = normalizedDraft(b);
  return (
    x.first_name === y.first_name && x.last_name === y.last_name && x.phone === y.phone &&
    x.profession === y.profession && x.linkedin_url === y.linkedin_url &&
    x.interest_slugs.length === y.interest_slugs.length &&
    x.interest_slugs.every((slug, index) => slug === y.interest_slugs[index]) &&
    x.company === y.company && sameCard(x.interest_card, y.interest_card)
  );
}

// The same rules as registration (and the server re-checks them all): fast
// feedback only. Keys are the field ids; values are texts for `t()`.
// `savedCard`: the interest card as loaded, whose retired answers may be kept.
export function validateProfile(draft: ProfileDraft, savedCard?: InterestCard): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!draft.first_name.trim()) errors.first_name = "Enter your first name.";
  if (!draft.last_name.trim()) errors.last_name = "Enter your last name.";
  if (!draft.phone.trim()) errors.phone = "Enter your phone number.";
  if (!draft.profession.trim()) errors.profession = "Enter your profession or role.";
  const linkedin = draft.linkedin_url.trim();
  if (linkedin && !isLinkedInUrl(linkedin)) {
    errors.linkedin_url = "Enter a full LinkedIn URL, e.g. https://www.linkedin.com/in/your-name";
  }
  // The same company and card rules as registration (one shared check).
  Object.assign(errors, companyAndCardErrors(draft.company, draft.interest_card, savedCard));
  return errors;
}

export function updateProfile(
  draft: ProfileDraft,
  expectedVersion: number,
): Promise<ApiResult<AttendeeProfile>> {
  const d = normalizedDraft(draft);
  return apiRequest<AttendeeProfile>("/api/attendees/me/profile", "PUT", {
    first_name: d.first_name,
    last_name: d.last_name,
    phone: d.phone,
    profession: d.profession,
    linkedin_url: d.linkedin_url === "" ? null : d.linkedin_url,
    interest_slugs: d.interest_slugs,
    company: d.company === "" ? null : d.company,
    interest_card: d.interest_card,
    expected_version: expectedVersion,
  }).then((result) => (result.ok ? { ok: true, data: normalizeProfile(result.data) } : result));
}

export type FieldDiff = { field: keyof ProfileDraft; mine: string; theirs: string };

// Where the typed draft and the profile as it now stands on the server differ -
// what a conflict screen shows so the person can decide.
export function diffDrafts(
  mine: ProfileDraft,
  theirs: ProfileDraft,
  interestName: (slug: string) => string,
  translate: (text: string) => string = (text) => text,
): FieldDiff[] {
  const a = normalizedDraft(mine);
  const b = normalizedDraft(theirs);
  const diffs: FieldDiff[] = [];
  for (const field of ["first_name", "last_name", "phone", "profession", "company", "linkedin_url"] as const) {
    if (a[field] !== b[field]) diffs.push({ field, mine: a[field], theirs: b[field] });
  }
  if (a.interest_slugs.join("|") !== b.interest_slugs.join("|")) {
    diffs.push({
      field: "interest_slugs",
      mine: a.interest_slugs.map(interestName).join(", "),
      theirs: b.interest_slugs.map(interestName).join(", "),
    });
  }
  if (!sameCard(a.interest_card, b.interest_card)) {
    diffs.push({
      field: "interest_card",
      mine: cardSummary(a.interest_card, translate),
      theirs: cardSummary(b.interest_card, translate),
    });
  }
  return diffs;
}

// The card in one line ("Banking and finance; Data, Fintech; ..."), for a
// conflict screen.
export function cardSummary(card: InterestCard, translate: (text: string) => string): string {
  return CARD_GROUPS.map((g) => selectedIn(card, g.key).map((id) => translate(labelOf(g.key, id))).join(", "))
    .filter(Boolean)
    .join("; ");
}

// --- sponsor consent -------------------------------------------------------------

export type ConsentState = {
  sponsor_consent: boolean;
  sponsor_consent_at: string | null;
  consent_version: string | null;
  // false: it was already the value asked for - nothing was written, no audit row.
  changed: boolean;
};

export function setSponsorConsent(granted: boolean): Promise<ApiResult<ConsentState>> {
  return apiRequest<ConsentState>("/api/attendees/me/sponsor-consent", "PUT", { granted });
}

// --- the photo -----------------------------------------------------------------------

export const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;
// The server's default limit (MEDIA_MAX_UPLOAD_BYTES); it stays the authority.
export const MAX_PHOTO_BYTES = 8 * 1024 * 1024;

// A check on the chosen file that spares a pointless upload. The server looks at
// the bytes, not at this: it re-checks everything.
export function photoProblem(file: { type: string; size: number }): string | null {
  if (!(PHOTO_TYPES as readonly string[]).includes(file.type)) {
    return "Choose a JPEG, PNG or WebP picture.";
  }
  if (file.size === 0) return "That file is empty. Choose another picture.";
  if (file.size > MAX_PHOTO_BYTES) return "That photo is too large. Choose a smaller picture.";
  return null;
}

export type PhotoResponse = { photo: PhotoUrls | null };

export function uploadPhoto(file: Blob): Promise<ApiResult<PhotoResponse>> {
  return apiPutBinary<PhotoResponse>("/api/attendees/me/photo", file).then((result) =>
    result.ok ? { ok: true, data: { photo: photoUrlsFrom(result.data.photo) } } : result,
  );
}

export function removePhoto(): Promise<ApiResult<PhotoResponse>> {
  return apiRequest<PhotoResponse>("/api/attendees/me/photo", "DELETE");
}
