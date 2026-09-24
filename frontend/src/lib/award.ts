// Staff task award (ARCHITECTURE.md §12.2, §13.1 step 7, §13.4). The
// request carries only what the canonical contract allows — card
// token, points, idempotency key, scan_ref — never a booth, staff id
// or source: those are derived server-side and there is no field here
// to express them even by accident.

import { apiPost, type ApiResult } from "./api";

export type AwardRequest = {
  card_token: string;
  points: number;
  idempotency_key: string;
  scan_ref: string;
};

export type AwardResponse = {
  points_log_id: number;
  points_awarded: number;
  resulting_total: number;
  attendee_name: string;
  booth_name: string;
  awarded_at: string;
  replayed: boolean;
};

export function awardPoints(request: AwardRequest): Promise<ApiResult<AwardResponse>> {
  return apiPost<AwardResponse>("/api/staff/points/award", request);
}
