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
    <section aria-label="Your summit score" className="relative overflow-hidden rounded-3xl border border-cyan-300/25 bg-slate-900 p-6 shadow-xl shadow-black/10">
      <div aria-hidden className="absolute -right-10 -top-10 h-36 w-36 rounded-full bg-cyan-300/10 blur-3xl" />
      <p className="relative text-xs font-bold uppercase tracking-[0.2em] text-cyan-300">
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
          <p className="relative mt-3 text-5xl font-bold tracking-tight text-white tabular-nums">
            {state.score.total_points}{" "}
            <span className="text-sm font-normal text-slate-400">pts</span>
          </p>
          <p className="mt-2 text-sm font-medium text-slate-200">Rank #{state.score.rank}</p>
          {state.justAwardedPoints !== null && (
            <p role="status" className="mt-3 text-sm font-semibold text-emerald-300 motion-safe:animate-pulse">
              +{state.justAwardedPoints} points!
            </p>
          )}
        </>
      )}
    </section>
  );
}
