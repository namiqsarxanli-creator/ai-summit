import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { LeaderboardView } from "@/components/leaderboard/LeaderboardView";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";

export const metadata: Metadata = {
  title: "Leaderboard",
};

// M13 wires M08's honest "not yet available" placeholder to the real
// leaderboard (§16) — `LeaderboardView` does its own fetching/session
// handling, the same independent-per-surface pattern every other
// M06+ page uses.
export default function LeaderboardPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-leaderboard">
      <PageHeader showBack={false} title="Leaderboard" description="Discover more. Earn more points." kind="leaderboard" />
      <LeaderboardView />
      <AttendeeTabBar active="leaderboard" />
      <BrandCredit />
    </main>
  );
}
