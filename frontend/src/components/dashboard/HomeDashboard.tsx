"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ScoreSummary } from "@/components/dashboard/ScoreSummary";
import { RecommendationCard } from "@/components/matching/RecommendationCard";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { fetchRecommendations, isGeneralListing, type Recommendation } from "@/lib/matching";

const TOP_N = 3;

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; recommendations: Recommendation[] }
  | { kind: "error"; code: string };

function stateFrom(result: ApiResult<Recommendation[]>): State {
  if (result.ok) return { kind: "ok", recommendations: result.data };
  const { code } = result.error;
  return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
    ? { kind: "signed-out" }
    : { kind: "error", code };
}

// M08's Home surface: "welcome/profile summary and matched booths".
// The identity/profile summary is `SessionPanel` (M05, already on this
// page); this component adds the matched-booths preview and the tab
// bar. The points summary (M13, `ScoreSummary`) fetches its own real
// score independently, the same way `FeedbackForm` (M12) independently
// checks its own session — this component's recommendations fetch is
// unrelated. It fetches its OWN recommendations data — the same M07
// already exposes — rather than sharing SessionPanel's session fetch,
// mirroring how every other M06/M07 surface independently
// authenticates itself from its own endpoint.
export function HomeDashboard() {
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
    return <p className="text-sm text-slate-500">Loading your dashboard…</p>;
  }

  // SessionPanel already offers the register/login call to action for
  // a signed-out visitor — nothing else belongs on the anonymous home
  // screen, and certainly not a tab bar for surfaces that need a
  // session.
  if (state.kind === "signed-out") {
    return null;
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="w-full text-sm text-amber-300">
        {messageFor(state.code)}
      </p>
    );
  }

  const { recommendations } = state;
  const top = recommendations.slice(0, TOP_N);
  const general = isGeneralListing(recommendations);

  return (
    <div className="flex w-full flex-col gap-6 text-left">
      <ScoreSummary />

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
            Your top matches
          </h2>
          <Link href="/booths" className="text-sm font-medium text-indigo-300 hover:text-indigo-200">
            See all →
          </Link>
        </div>
        {top.length === 0 ? (
          <p className="text-sm text-slate-500">No booths are available yet. Check back soon.</p>
        ) : (
          <>
            {general && (
              <p className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-sm text-slate-400">
                Select interests when you register to see your personalised match for each booth.
              </p>
            )}
            <ul className="flex flex-col gap-3">
              {top.map((r) => (
                <RecommendationCard key={r.booth.slug} recommendation={r} />
              ))}
            </ul>
          </>
        )}
      </section>

      <AttendeeTabBar active="home" />
    </div>
  );
}
