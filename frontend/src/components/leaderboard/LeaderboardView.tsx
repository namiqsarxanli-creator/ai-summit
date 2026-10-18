"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";
import { useState } from "react";

import { Avatar } from "@/components/ui/Avatar";
import { messageFor } from "@/lib/errors";
import { fetchLeaderboardPage, type LeaderboardEntry } from "@/lib/leaderboard";
import { useLeaderboardStore } from "@/lib/realtime";

type AllState =
  | { kind: "closed" }
  | { kind: "loading" }
  | { kind: "error"; code: string }
  | {
      kind: "ok";
      entries: LeaderboardEntry[];
      nextCursor: string | null;
      datasetGeneration: number;
      asOf: string;
      loadingMore: boolean;
    };

function Row({ entry, highlight }: { entry: LeaderboardEntry; highlight: boolean }) {
  const { t } = useLanguage();
  return (
    <li data-rank={entry.rank} data-current={highlight} className="fm-rank-row">
      <div className="fm-rank-main">
        <span className="fm-rank-position">#{entry.rank}</span>
        <Avatar size="rank" url={entry.photo_url} />
        <div className="fm-grow">
          <p className="fm-rank-name">{entry.name}</p>
          <p className="fm-rank-role">{entry.profession}</p>
        </div>
      </div>
      <span className="fm-rank-points">{entry.total_points} {t("pts")}</span>
    </li>
  );
}

// M13 wired the leaderboard tab to real data (§16); M14 wires the
// default top-N view to the shared realtime owner (`lib/realtime.ts`)
// instead of its own one-shot fetch, so it updates live when another
// attendee scores (§17.4: "another attendee scoring can change the
// current attendee's rank" — a personal `state_changed` event alone
// would never tell this view that). `current_user` is always returned
// by `/api/leaderboard`'s default view whether the attendee is inside
// or outside the top N (§16.3) — that is what spares this component a
// second fetch, not identical markup for both cases: inside the top N
// their row is simply highlighted in place; outside it, the same row
// is additionally shown as "Your position". The "show all" view below
// keeps its own independent pagination state, untouched by realtime
// reconciliation (§16.3's keyset contract is preserved as-is).
export function LeaderboardView() {
  const { t, language } = useLanguage();
  const top = useLeaderboardStore();
  const [all, setAll] = useState<AllState>({ kind: "closed" });

  function loadAllFromStart() {
    setAll({ kind: "loading" });
    fetchLeaderboardPage(null).then((result) => {
      if (!result.ok) {
        setAll({ kind: "error", code: result.error.code });
        return;
      }
      setAll({
        kind: "ok",
        entries: result.data.entries,
        nextCursor: result.data.next_cursor,
        datasetGeneration: result.data.dataset_generation,
        asOf: result.data.as_of,
        loadingMore: false,
      });
    });
  }

  function loadMore() {
    if (all.kind !== "ok" || all.nextCursor === null) return;
    const current = all;
    setAll({ ...current, loadingMore: true });
    fetchLeaderboardPage(current.nextCursor).then((result) => {
      if (!result.ok) {
        setAll({ kind: "error", code: result.error.code });
        return;
      }
      // §16.3: "the client ... discards a page whose dataset_generation
      // differs from the first page's, restarting the listing."
      if (result.data.dataset_generation !== current.datasetGeneration) {
        loadAllFromStart();
        return;
      }
      setAll({
        kind: "ok",
        entries: [...current.entries, ...result.data.entries],
        nextCursor: result.data.next_cursor,
        datasetGeneration: current.datasetGeneration,
        asOf: result.data.as_of,
        loadingMore: false,
      });
    });
  }

  if (top.status === "loading") {
    return <p role="status" className="fm-empty">{t("Loading leaderboard…")}</p>;
  }

  if (top.status === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in to see the leaderboard.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Sign in")}
        </Link>
      </div>
    );
  }

  if (top.status === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(top.code))}
      </p>
    );
  }

  const { entries, current_user: currentUser } = top.leaderboard;
  const currentUserInTop =
    currentUser !== null && entries.some((e) => e.user_id === currentUser.user_id);

  return (
    <div className="fm-stack-lg">
      {entries.length === 0 ? (
        <p className="fm-empty">{t("No attendees have joined the leaderboard yet. Your position will appear here when the event begins.")}</p>
      ) : (
        <ul className="fm-list" style={{ gap: 8 }}>
          {entries.map((e) => (
            <Row
              key={e.user_id}
              entry={e}
              highlight={currentUser !== null && e.user_id === currentUser.user_id}
            />
          ))}
        </ul>
      )}

      {currentUser !== null && !currentUserInTop && (
        <div className="fm-section">
          <p className="fm-eyebrow">{t("Your position")}</p>
          <Row entry={currentUser} highlight />
        </div>
      )}

      {all.kind === "closed" && (
        <button
          type="button"
          onClick={loadAllFromStart}
          className="fm-btn fm-btn-secondary fm-btn-block"
        >
          {t("Show all")}
        </button>
      )}

      {all.kind === "loading" && (
        <p role="status" className="fm-note">{t("Loading full leaderboard…")}</p>
      )}

      {all.kind === "error" && (
        <p role="alert" className="fm-alert fm-alert-warn">
          {t(messageFor(all.code))}
        </p>
      )}

      {all.kind === "ok" && (
        <div className="fm-section" style={{ borderTop: "1px solid var(--fm-line)", paddingTop: 16 }}>
          <p className="fm-micro">{t("As of")} {new Date(all.asOf).toLocaleTimeString(language)}</p>
          <ul className="fm-list" style={{ gap: 8 }}>
            {all.entries.map((e) => (
              <Row
                key={e.user_id}
                entry={e}
                highlight={currentUser !== null && e.user_id === currentUser.user_id}
              />
            ))}
          </ul>
          {all.nextCursor !== null && (
            <button
              type="button"
              onClick={loadMore}
              disabled={all.loadingMore}
              className="fm-btn fm-btn-secondary fm-btn-block"
            >
              {all.loadingMore ? t("Loading…") : t("Load more")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
