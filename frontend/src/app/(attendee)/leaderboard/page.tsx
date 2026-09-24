import type { Metadata } from "next";
import Link from "next/link";

import { LeaderboardView } from "@/components/leaderboard/LeaderboardView";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";

export const metadata: Metadata = {
  title: "Leaderboard · FACEMARK",
};

// M13 wires M08's honest "not yet available" placeholder to the real
// leaderboard (§16) — `LeaderboardView` does its own fetching/session
// handling, the same independent-per-surface pattern every other
// M06+ page uses.
export default function LeaderboardPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col gap-8 px-4 py-10 pb-24 sm:px-6">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Leaderboard
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Leaderboard</h1>
        <p className="text-sm text-slate-400">
          <Link href="/" className="font-medium text-indigo-300 hover:text-indigo-200">
            ← Home
          </Link>
        </p>
      </header>
      <LeaderboardView />
      <AttendeeTabBar active="leaderboard" />
    </main>
  );
}
