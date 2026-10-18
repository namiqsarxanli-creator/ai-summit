"use client";

import Link from "next/link";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { HomeIcon } from "@/components/ui/HomeIcons";
import { useEffect, useState } from "react";

import { ScoreSummary } from "@/components/dashboard/ScoreSummary";
import { RecommendationCard } from "@/components/matching/RecommendationCard";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { fetchRecommendations, isGeneralListing, type Recommendation } from "@/lib/matching";

const TOP_N = 3;

// The Beyond Now programme surfaces, in the order an attendee uses them.
const HUB = [
  { href: "/programme", icon: "calendar", title: "My programme", detail: "Your sessions and reminders" },
  { href: "/masterclasses", icon: "masterclass", title: "Masterclasses", detail: "Reserve a seat or join the waitlist" },
  { href: "/playground", icon: "playground", title: "AI Playground", detail: "Book a hands-on slot" },
  { href: "/people", icon: "people", title: "People to meet", detail: "Matches for a conversation" },
  { href: "/points", icon: "star", title: "My points", detail: "How you earned every point" },
  { href: "/report", icon: "report", title: "My report", detail: "Your day in one place" },
] as const;

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
    return <p role="status" className="fm-note">{t("Loading your dashboard…")}</p>;
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
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { recommendations } = state;
  const top = recommendations.slice(0, TOP_N);
  const general = isGeneralListing(recommendations);

  return (
    <div className="fm-stack">
      <ScoreSummary />

      <div className="fm-actions" aria-label={t("Quick actions")}>
        <Link href="/booths" className="fm-action fm-action-booths">
          <span className="fm-action-title">{t("Booths")}</span>
          <span className="fm-action-detail">{t("Discover sponsors and join activities")}</span>
          <span className="fm-action-arrow"><HomeIcon name="arrow" /></span>
        </Link>
        <Link href="/my-card" className="fm-action fm-action-qr">
          <span className="fm-action-title">{t("My QR")}</span>
          <span className="fm-action-detail">{t("Make event participation easier")}</span>
          <span className="fm-action-arrow"><HomeIcon name="arrow" /></span>
        </Link>
      </div>

      <section className="fm-section" aria-labelledby="home-beyond-now">
        <h2 id="home-beyond-now" className="fm-eyebrow">{t("Beyond Now")}</h2>
        <ul className="fm-hub">
          {HUB.map((item) => (
            <li key={item.href}>
              <Link href={item.href} className="fm-hub-link">
                <span className="fm-hub-icon"><HomeIcon name={item.icon} /></span>
                <span className="fm-hub-text">
                  <span className="fm-hub-title">{t(item.title)}</span>
                  <span className="fm-hub-detail">{t(item.detail)}</span>
                </span>
                <span className="fm-hub-arrow"><HomeIcon name="arrow" /></span>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <section className="fm-section">
        <div className="fm-row-between">
          <h2 className="fm-eyebrow">{t("Your top matches")}</h2>
          <Link href="/booths" className="fm-link fm-link-block">
            {t("See all →")}
          </Link>
        </div>
        {top.length === 0 ? (
          <p className="fm-empty">{t("No booths are available yet. Check back soon.")}</p>
        ) : (
          <>
            {general && (
              <p className="fm-alert fm-alert-info">
                {t("Choose topics on your interest card in your profile to see your personalised match for each booth.")}
              </p>
            )}
            <ul className="fm-list">
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
