"use client";

import { messageFor } from "@/lib/errors";
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
  const state = useScoreStore();

  if (state.status === "signed-out") return null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
        Your points
      </p>
      {state.status === "loading" && <p className="mt-1 text-sm text-slate-400">Loading…</p>}
      {state.status === "error" && (
        <p role="alert" className="mt-1 text-sm text-amber-300">
          {messageFor(state.code)}
        </p>
      )}
      {state.status === "ok" && (
        <>
          <p className="mt-1 text-2xl font-semibold text-slate-50">
            {state.score.total_points}{" "}
            <span className="text-sm font-normal text-slate-400">pts</span>
          </p>
          <p className="text-sm text-slate-400">Rank #{state.score.rank}</p>
          {state.justAwardedPoints !== null && (
            <p role="status" className="mt-1 text-sm font-semibold text-emerald-400">
              +{state.justAwardedPoints} points!
            </p>
          )}
        </>
      )}
    </section>
  );
}
