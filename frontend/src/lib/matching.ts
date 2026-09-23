// Booth catalogue and matching (ARCHITECTURE.md §10, §11). Scores,
// matched interests and explanations are rendered exactly as the server
// returns them — no client-side calculation, no cosmetic transform
// (§24.5, §24.9: "no business logic in components").

import { apiGet, type ApiResult } from "@/lib/api";

export type Booth = { slug: string; name: string; location_label: string | null };

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

// Homogeneous by construction on the server: when interests are unset,
// EVERY recommendation carries `match: null`; otherwise none do
// (§11.4). This only names that fact — it computes nothing.
export function isGeneralListing(recommendations: Recommendation[]): boolean {
  return recommendations.length > 0 && recommendations.every((r) => r.match === null);
}
