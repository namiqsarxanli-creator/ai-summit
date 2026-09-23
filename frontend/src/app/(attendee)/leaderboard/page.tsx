import type { Metadata } from "next";
import Link from "next/link";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";

export const metadata: Metadata = {
  title: "Leaderboard · FACEMARK",
};

// IMPLEMENTATION_PLAN.md M08: "the Leaderboard tab ... ships as a
// defined, disabled placeholder that renders an honest 'not yet
// available' state, and M13 wires it to real data." Score and rank
// require points_log (M11) and its aggregation (M13) — neither exists
// yet, so there is nothing here to fetch, and nothing to fabricate.
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
      <p className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 text-sm text-slate-400">
        The leaderboard isn&apos;t available yet. Points start counting once the summit opens —
        check back soon.
      </p>
      <AttendeeTabBar active="leaderboard" />
    </main>
  );
}
