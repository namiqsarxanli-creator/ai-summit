// Beyond Now Photo: pair photos of connected people. The server checks the pair,
// re-encodes the image and decides what may be shown on the big screen.

import { apiGet, apiPost, apiPostBinary, type ApiResult } from "@/lib/api";

export type PairPhoto = {
  id: number;
  status: "pending" | "accepted";
  role: "uploader" | "partner";
  with: string;
  photo_url: string | null;
  thumb_url: string | null;
  my_screen_consent: boolean;
  their_screen_consent: boolean;
  moderation: "pending" | "approved" | "rejected";
  on_screen: boolean;
};

const BASE = "/api/attendees/me/pair-photos";

export const fetchPairPhotos = () => apiGet<{ photos: PairPhoto[] }>(BASE);
export const uploadPairPhoto = (partnerId: number, screen: boolean, file: Blob) =>
  apiPostBinary<PairPhoto>(`${BASE}?partner_id=${encodeURIComponent(String(partnerId))}&screen=${screen ? "true" : "false"}`, file);
export const answerPairPhoto = (id: number, accept: boolean, screen: boolean) =>
  apiPost<PairPhoto>(`${BASE}/${id}/answer`, { accept, screen });
export const screenConsent = (id: number, consent: boolean) => apiPost<PairPhoto>(`${BASE}/${id}/screen`, { consent });
export const withdrawPairPhoto = (id: number) => apiPost<PairPhoto>(`${BASE}/${id}/withdraw`, {});
export const fetchFeed = () => apiGet<{ photos: { photo_url: string }[] }>("/api/pair-photos/feed");

// Only URLs the server itself makes are ever put in an <img>. Pair photos have
// their own, authorised route (never the public avatar one).
const PAIR_URL = /^\/api\/media\/pair\/[0-9a-f]{32}-(?:128|384)\.jpg$/;
export const safePhotoUrl = (url: string | null | undefined) =>
  typeof url === "string" && PAIR_URL.test(url) ? url : null;

const REASONS: Record<string, string> = {
  not_connected: "Scan each other's QR first: only connected people can share a photo.",
  self: "Choose the other person in the photo.",
  too_many: "You have shared the maximum number of photos.",
  already_answered: "This photo has already been answered.",
  withdrawn: "This photo was withdrawn.",
  not_accepted: "Accept the photo first.",
};

export function pairPhotoFailure(result: Extract<ApiResult<unknown>, { ok: false }>, messageFor: (code: string) => string): string {
  const reason = result.error.details.reason;
  if (typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  if (result.error.code === "PAIR_PHOTO_NOT_FOUND") return "This photo is not available.";
  return messageFor(result.error.code);
}
