// The leaderboard (ARCHITECTURE.md §16): a default top-N view (with
// the caller's own row attached even when outside it, §16.3) and a
// keyset-paginated "show all" view. No OFFSET pagination exists
// anywhere in this client — `next_cursor` is opaque and only ever
// round-tripped, never constructed or inspected here.

import { apiGet, type ApiResult } from "@/lib/api";

export type LeaderboardEntry = {
  user_id: number;
  first_name: string;
  last_name: string;
  profession: string;
  total_points: number;
  rank: number;
};

export type LeaderboardResponse = {
  entries: LeaderboardEntry[];
  current_user: LeaderboardEntry | null;
  as_of: string;
  dataset_generation: number;
  next_cursor: string | null;
};

export function fetchLeaderboardTop(): Promise<ApiResult<LeaderboardResponse>> {
  return apiGet<LeaderboardResponse>("/api/leaderboard");
}

export function fetchLeaderboardPage(
  cursor: string | null,
): Promise<ApiResult<LeaderboardResponse>> {
  const params = new URLSearchParams({ view: "all" });
  if (cursor) params.set("cursor", cursor);
  return apiGet<LeaderboardResponse>(`/api/leaderboard?${params.toString()}`);
}
