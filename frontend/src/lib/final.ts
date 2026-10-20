// The final result: 17:30 close, Game Master review, 17:40 Grand Final. The
// server ranks, freezes and publishes; this file only reads and asks.

import { apiGet, apiPost, type ApiResult } from "@/lib/api";

export type PublicPerson = { name: string; photo_url: string | null; place?: number; points?: number };
export type PublicFinal =
  // not_configured: no event or no close yet; disabled: the ranking is switched off.
  | { status: "not_configured" | "disabled" | "open" | "pending_review"; closes_at: string | null; missing?: string[] }
  | { status: "published"; closes_at: string | null; version: number; published_at: string | null; top: PublicPerson[]; founders: PublicPerson[]; founder_rule_confirmed?: boolean };

export type VersionSummary = {
  id: number;
  version: number;
  status: "draft" | "published" | "superseded" | "discarded";
  created_at: string;
  published_at: string | null;
  entries: number;
  founders: number;
};
export type RankedRow = { place: number; user_id: number; name: string; points: number; reached_at: string | null };
export type FinalDetail = VersionSummary & {
  ranking: RankedRow[];
  founder_list: { user_id: number; name: string; founder_at: string }[];
  ledger_changed_since: boolean;
};

export const fetchFinal = () => apiGet<PublicFinal>("/api/final");
export type Versions = { closes_at: string | null; timezone: string; pending_offline_scans: number; versions: VersionSummary[] };
export const fetchVersions = () => apiGet<Versions>("/api/staff/final");
export const fetchVersion = (id: number) => apiGet<FinalDetail>(`/api/staff/final/${id}`);
export const takeSnapshot = () => apiPost<VersionSummary>("/api/staff/final", {});
export const publishVersion = (id: number) => apiPost<VersionSummary>(`/api/staff/final/${id}/publish`, {});
export const discardVersion = (id: number) => apiPost<VersionSummary>(`/api/staff/final/${id}/discard`, {});
export const foundersCsvUrl = (id: number) => `/api/staff/final/${id}/founders.csv`;

const REASONS: Record<string, string> = {
  competition_open: "The competition is still open. A final result can be taken after the close.",
  not_a_draft: "Only a draft can be published or discarded.",
  newer_version_published: "A newer version is already published.",
  stale_snapshot: "Points changed after this snapshot. Take a new snapshot, check it, and publish that one.",
  offline_reviews_pending: "Late scans still wait for review. Approve or reject them, then take a new snapshot.",
  founder_rule_unconfirmed: "The Founder rule is not confirmed yet, so there is no Lucky Draw list.",
  not_configured: "The event is not set up yet.",
};

export function finalFailure(result: Extract<ApiResult<unknown>, { ok: false }>, messageFor: (code: string) => string): string {
  const reason = result.error.details.reason;
  if (typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  if (result.error.code === "FINAL_NOT_FOUND") return "This result does not exist.";
  return messageFor(result.error.code);
}

export const ZONE_LABEL: Record<string, string> = {
  main_stage: "Main Stage",
  masterclass: "Masterclass",
  playground: "AI Playground",
  connection_hub: "Connection Hub",
};
