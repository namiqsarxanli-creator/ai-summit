// Contact-card API (ARCHITECTURE.md §9). The shapes mirror the backend's
// explicit response schemas: the public card has no phone, no consent
// state and no id, and exactly one server-chosen contact action.

import { apiGet, type ApiResult } from "@/lib/api";

export type ContactAction = { type: "LINKEDIN" | "EMAIL"; value: string };

export type PublicCard = {
  first_name: string;
  last_name: string;
  profession: string;
  interests: { slug: string; name: string }[];
  contact: ContactAction;
};

export type OwnCard = { card_url: string; card: PublicCard };

export function fetchPublicCard(token: string): Promise<ApiResult<PublicCard>> {
  return apiGet<PublicCard>(`/api/cards/${encodeURIComponent(token)}`);
}

// The card is chosen by the session cookie on the server — this call
// carries no identity of its own.
export function fetchMyCard(): Promise<ApiResult<OwnCard>> {
  return apiGet<OwnCard>("/api/attendees/me/card");
}

// §9.2: the QR encodes ONLY `https://<host>/c/<43-char token>`. Checked
// again here before anything is encoded, so a server defect could never
// print something else into an attendee's code.
const CARD_URL = /^https?:\/\/[^/?#\s@]+\/c\/[A-Za-z0-9_-]{43}$/;

export function isCardUrl(value: string): boolean {
  return CARD_URL.test(value);
}

// A LinkedIn value is rendered as a link only if it is plainly http(s):
// never a `javascript:` or other scheme, whatever the data says.
export function safeExternalUrl(value: string): string | null {
  return /^https?:\/\//i.test(value) ? value : null;
}
