"use client";

import Link from "next/link";
import { useState } from "react";

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
  return (
    <li
      className={`flex min-w-0 items-center justify-between gap-3 rounded-2xl border px-4 py-4 ${
        highlight ? "border-cyan-300 bg-cyan-300/10" : "border-slate-700 bg-slate-900/80"
      }`}
    >
      <div className="flex min-w-0 items-center gap-3">
        <span className="w-10 shrink-0 text-lg font-bold tabular-nums text-cyan-200">#{entry.rank}</span>
        <div className="min-w-0">
          <p className="break-words text-sm font-semibold text-slate-100">
            {entry.first_name} {entry.last_name}
          </p>
          <p className="break-words text-xs text-slate-300">{entry.profession}</p>
        </div>
      </div>
      <span className="shrink-0 text-sm font-bold tabular-nums text-white">{entry.total_points} pts</span>
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
    return <p role="status" className="rounded-2xl border border-slate-700 bg-slate-900 p-5 text-sm text-slate-300">Loading leaderboard…</p>;
  }

  if (top.status === "signed-out") {
    return (
      <p className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 text-sm text-slate-400">
        <Link href="/login" className="font-medium text-indigo-300 hover:text-indigo-200">
          Sign in
        </Link>{" "}
        to see the leaderboard.
      </p>
    );
  }

  if (top.status === "error") {
    return (
      <p
        role="alert"
        className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 text-sm text-amber-300"
      >
        {messageFor(top.code)}
      </p>
    );
  }

  const { entries, current_user: currentUser } = top.leaderboard;
  const currentUserInTop =
    currentUser !== null && entries.some((e) => e.user_id === currentUser.user_id);

  return (
    <div className="flex flex-col gap-6">
      {entries.length === 0 ? (
        <p className="rounded-2xl border border-slate-700 bg-slate-900 p-5 text-sm text-slate-300">No attendees have joined the leaderboard yet. Your position will appear here when the event begins.</p>
      ) : (
        <ul className="flex flex-col gap-2">
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
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
            Your position
          </p>
          <Row entry={currentUser} highlight />
        </div>
      )}

      {all.kind === "closed" && (
        <button
          type="button"
          onClick={loadAllFromStart}
          className="min-h-11 self-start rounded-xl border border-slate-600 px-5 py-2 text-sm font-semibold text-slate-100 transition-colors hover:bg-slate-800"
        >
          Show all
        </button>
      )}

      {all.kind === "loading" && (
        <p className="text-sm text-slate-500">Loading full leaderboard…</p>
      )}

      {all.kind === "error" && (
        <p role="alert" className="text-sm text-amber-300">
          {messageFor(all.code)}
        </p>
      )}

      {all.kind === "ok" && (
        <div className="flex flex-col gap-3 border-t border-slate-800 pt-4">
          <p className="text-xs text-slate-500">As of {new Date(all.asOf).toLocaleTimeString()}</p>
          <ul className="flex flex-col gap-2">
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
              className="self-start rounded-lg border border-slate-700 px-4 py-2 text-sm font-semibold text-slate-200 transition-colors hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {all.loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
