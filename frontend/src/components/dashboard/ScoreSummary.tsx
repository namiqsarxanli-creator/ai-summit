"use client";

import { messageFor } from "@/lib/errors";
import { useLanguage } from "@/components/ui/LanguageProvider";

import { useScoreStore } from "@/lib/realtime";

// M13 wired M08's honest "points tracking starts once the summit
// opens" placeholder to the real authoritative snapshot; M14 wires
// THAT to the shared realtime owner (`lib/realtime.ts`) instead of its
// own one-shot fetch — the total and rank shown here are always
// exactly what the server returned in its latest snapshot (§24.1), and
// update live via SSE invalidation + refetch, never a client-side
// `total += points`. Mounted only from inside HomeDashboard's own "ok"
// (already-signed-in) branch; the "signed-out" state here exists only
// for the rare race where the session expires between HomeDashboard's
// own check and this store's first snapshot, and renders nothing for
// it rather than a scary error.
export function ScoreSummary() {
  const { t } = useLanguage();
  const state = useScoreStore();

  if (state.status === "signed-out") return null;

  return (
    <section aria-label={t("Your summit score")} className="fm-score-card">
      <span className="fm-score-medal" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z" /></svg></span>
      <p className="fm-score-label">{t("Collected points")}</p>
      {state.status === "loading" && <p role="status" className="fm-small">{t("Loading…")}</p>}
      {state.status === "error" && (
        <p role="alert" className="fm-error">
          {t(messageFor(state.code))}
        </p>
      )}
      {state.status === "ok" && (
        <>
          <p className="fm-score-value">
            {state.score.total_points} <span>{t("pts")}</span>
          </p>
          <p className="fm-score-rank">{t("Rank")} #{state.score.rank}</p>
          {state.justAwardedPoints !== null && (
            <p role="status" className="fm-status fm-status-ok motion-safe:animate-pulse">
              +{state.justAwardedPoints} {t("points!")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
