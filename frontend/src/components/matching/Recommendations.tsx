"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { fetchRecommendations, isGeneralListing, type Recommendation } from "@/lib/matching";

import { RecommendationCard } from "./RecommendationCard";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; recommendations: Recommendation[] }
  | { kind: "error"; code: string };

function stateFrom(result: ApiResult<Recommendation[]>): State {
  if (result.ok) return { kind: "ok", recommendations: result.data };
  const { code } = result.error;
  // §8.1: no matching capability before ACTIVE — both cases just mean
  // "not signed in yet" from this screen's point of view.
  return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
    ? { kind: "signed-out" }
    : { kind: "error", code };
}

// The attendee's booth recommendations (ARCHITECTURE.md §11,
// IMPLEMENTATION_PLAN.md M07). Identity comes from the session the
// backend resolves — this component never sends or knows an attendee id.
export function Recommendations() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchRecommendations().then((result) => {
      if (!cancelled) setState(stateFrom(result));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") {
    return <p className="text-sm text-slate-500">Loading your matches…</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-slate-400">Sign in to see your personalised booth matches.</p>
        <Link
          href="/login"
          className="w-full rounded-lg bg-indigo-500 px-4 py-3 text-center font-semibold text-white transition-colors hover:bg-indigo-400"
        >
          Sign in
        </Link>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="text-sm text-amber-300">
        {messageFor(state.code)}
      </p>
    );
  }

  const { recommendations } = state;

  if (recommendations.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <p className="text-sm text-slate-500">No booths are available yet. Check back soon.</p>
        <AttendeeTabBar active="booths" />
      </div>
    );
  }

  const general = isGeneralListing(recommendations);

  return (
    <div className="flex flex-col gap-4">
      <p
        className={
          general
            ? "rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-sm text-slate-400"
            : "text-sm text-slate-500"
        }
      >
        {general
          ? "Select interests when you register to see your personalised match for each booth. For now, here are all the booths at the summit."
          : "Ranked for you, based on the interests you selected."}
      </p>
      <ul className="flex flex-col gap-4">
        {recommendations.map((r) => (
          <RecommendationCard key={r.booth.slug} recommendation={r} />
        ))}
      </ul>
      <AttendeeTabBar active="booths" />
    </div>
  );
}
