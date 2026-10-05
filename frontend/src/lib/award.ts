// Staff task award (ARCHITECTURE.md §12.2, §13.1 step 7, §13.4). The
// request carries only what the canonical contract allows — card
// token, the activity, the rule revision the staff member confirmed, the
// idempotency key and scan_ref — never a point value, booth, staff id
// or source: those are derived server-side and there is no field here
// to express them even by accident.

import { apiPost, type ApiResult } from "./api";

export type AwardRequest = {
  card_token: string;
  task_id: number;
  // The server-issued revision of the rule (points + repeat policy) that was
  // on screen when the staff member confirmed. It confirms; it cannot set.
  rule_revision: string;
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
  // Absent on a replay of an award recorded before activities existed.
  task_id?: number | null;
  task_title?: string | null;
};

export function awardPoints(request: AwardRequest): Promise<ApiResult<AwardResponse>> {
  return apiPost<AwardResponse>("/api/staff/points/award", request);
}
