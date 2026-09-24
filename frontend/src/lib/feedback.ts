// Attendee feedback submission (ARCHITECTURE.md §15). The request
// carries only what the canonical contract allows — booth slug, rating,
// optional comment — never a user id, booth id, points or source:
// those are all server-derived and there is no field here to express
// them even by accident.

import { apiPost, type ApiResult } from "./api";

export type FeedbackRequest = {
  booth_slug: string;
  rating: number;
  comment?: string;
};

export type FeedbackResponse = {
  feedback_id: number;
  awarded_points: number;
  already_rewarded: boolean;
  total_points: number;
};

export function submitFeedback(request: FeedbackRequest): Promise<ApiResult<FeedbackResponse>> {
  return apiPost<FeedbackResponse>("/api/attendees/feedback", request);
}
