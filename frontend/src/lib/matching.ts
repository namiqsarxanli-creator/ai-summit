// Booth catalogue and matching (ARCHITECTURE.md §10, §11). Scores,
// matched interests and explanations are rendered exactly as the server
// returns them — no client-side calculation, no cosmetic transform
// (§24.5, §24.9: "no business logic in components").

import { apiGet, type ApiResult } from "@/lib/api";

// `description`: the booth's one-sentence card (optional: an older server sends none).
export type Booth = { slug: string; name: string; location_label: string | null; description?: string | null };

export type MatchedInterest = { slug: string; name: string };

export type Match = {
  score: number;
  matched_interests: MatchedInterest[];
  explanation: string;
  algorithm_version: string;
};

// `match` is `null` exactly when the attendee has selected no interests
// yet (§11.4) — a distinct absence of data, never a fake 0.
export type Recommendation = { booth: Booth; match: Match | null };

export function fetchRecommendations(): Promise<ApiResult<Recommendation[]>> {
  return apiGet<Recommendation[]>("/api/attendees/me/recommendations");
}

export function fetchBooths(): Promise<ApiResult<Booth[]>> {
  return apiGet<Booth[]>("/api/booths");
}

// What the Booths page and the home page list: the recommendations first (best
// match first, as the server ranks them), then every other active booth with no
// match. The server leaves a booth without an interest profile out of the
// recommendations entirely; without this, an event whose booths have no
// interests yet would show an empty Booths page. The catalogue failing still
// leaves the recommendations.
export async function fetchBoothListing(): Promise<ApiResult<Recommendation[]>> {
  const recommended = await fetchRecommendations();
  if (!recommended.ok) return recommended;
  const catalogue = await fetchBooths();
  if (!catalogue.ok || !Array.isArray(catalogue.data)) return recommended;
  const listed = new Set(recommended.data.map((r) => r.booth.slug));
  const rest = catalogue.data
    .filter((b) => typeof b?.slug === "string" && typeof b?.name === "string" && !listed.has(b.slug))
    .map((booth) => ({ booth, match: null }));
  return { ...recommended, data: [...recommended.data, ...rest] };
}

// M08's booth detail screen ("tasks and location"). A task carries no
// point value — that is a staff-side preset allow-list, not part of
// its own description, and not this milestone's to expose.
export type BoothTask = { title: string; description: string | null };

export type BoothDetail = Booth & { description: string | null; tasks: BoothTask[] };

export function fetchBoothDetail(slug: string): Promise<ApiResult<BoothDetail>> {
  return apiGet<BoothDetail>(`/api/booths/${encodeURIComponent(slug)}`);
}

// Homogeneous by construction on the server: when interests are unset,
// EVERY recommendation carries `match: null`; otherwise none do
// (§11.4). This only names that fact — it computes nothing.
export function isGeneralListing(recommendations: Recommendation[]): boolean {
  return recommendations.length > 0 && recommendations.every((r) => r.match === null);
}
