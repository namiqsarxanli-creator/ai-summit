"use client";

import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { fetchMyScore } from "@/lib/score";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; totalPoints: number; rank: number }
  | { kind: "error"; code: string };

// M13 wires M08's honest "points tracking starts once the summit
// opens" placeholder to the real authoritative snapshot (§17.4) — the
// total and rank shown here are exactly what the server returned, no
// client-side computation (§24.1). Mounted only from inside
// HomeDashboard's own "ok" (already-signed-in) branch; the
// "signed-out" state here exists only for the rare race where the
// session expires between HomeDashboard's fetch and this one, and
// renders nothing for it rather than a scary error.
export function ScoreSummary() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchMyScore().then((result) => {
      if (cancelled) return;
      if (!result.ok) {
        const { code } = result.error;
        setState(
          code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
            ? { kind: "signed-out" }
            : { kind: "error", code },
        );
        return;
      }
      setState({ kind: "ok", totalPoints: result.data.total_points, rank: result.data.rank });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "signed-out") return null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
        Your points
      </p>
      {state.kind === "loading" && <p className="mt-1 text-sm text-slate-400">Loading…</p>}
      {state.kind === "error" && (
        <p role="alert" className="mt-1 text-sm text-amber-300">
          {messageFor(state.code)}
        </p>
      )}
      {state.kind === "ok" && (
        <>
          <p className="mt-1 text-2xl font-semibold text-slate-50">
            {state.totalPoints} <span className="text-sm font-normal text-slate-400">pts</span>
          </p>
          <p className="text-sm text-slate-400">Rank #{state.rank}</p>
        </>
      )}
    </section>
  );
}
