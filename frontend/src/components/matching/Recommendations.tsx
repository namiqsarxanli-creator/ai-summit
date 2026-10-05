"use client";

import Link from "next/link";
import { useLanguage } from "@/components/ui/LanguageProvider";
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
  const { t } = useLanguage();
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
    return <p role="status" className="fm-note">{t("Loading your matches…")}</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in to see your personalised booth matches.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Sign in")}
        </Link>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { recommendations } = state;

  if (recommendations.length === 0) {
    return (
      <div className="fm-stack">
        <p className="fm-empty">{t("No booths are available yet. Check back soon.")}</p>
        <AttendeeTabBar active="booths" />
      </div>
    );
  }

  const general = isGeneralListing(recommendations);

  return (
    <div className="fm-stack">
      <p className={general ? "fm-alert fm-alert-info" : "fm-note"}>
        {general
          ? t("Select interests when you register to see your personalised match for each booth. For now, here are all the booths at the summit.")
          : t("Ranked for you, based on the interests you selected.")}
      </p>
      <ul className="fm-list">
        {recommendations.map((r) => (
          <RecommendationCard key={r.booth.slug} recommendation={r} />
        ))}
      </ul>
      <AttendeeTabBar active="booths" />
    </div>
  );
}
