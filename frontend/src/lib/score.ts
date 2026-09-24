// The authoritative personal score snapshot (ARCHITECTURE.md §17.4).
// Every value here is server-computed on every request — no caching,
// no client-side arithmetic, no denormalised total (§16.4, §24.1).

import { apiGet, type ApiResult } from "@/lib/api";

export type RecentAward = {
  id: number;
  points: number;
  booth_name: string;
  source: "TASK" | "FEEDBACK";
  created_at: string;
};

export type ScoreSnapshot = {
  total_points: number;
  rank: number;
  // Most recent 10, `created_at DESC, id DESC` — display order only,
  // never a delivery guarantee (§17.4).
  recent_awards: RecentAward[];
  as_of: string;
  dataset_generation: number;
};

export function fetchMyScore(): Promise<ApiResult<ScoreSnapshot>> {
  return apiGet<ScoreSnapshot>("/api/attendees/me/score");
}
