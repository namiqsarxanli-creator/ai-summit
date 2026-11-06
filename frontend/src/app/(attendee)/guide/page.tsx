import type { Metadata } from "next";

import { GameGuide } from "@/components/guide/GameGuide";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "How to play",
};

// The participant guide: readable before signing in, linked from Home and My points.
export default function GuidePage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="How to play" description="How to earn your points and the Beyond Now Founder title" kind="leaderboard" />
      <GameGuide />
      <BrandCredit />
    </main>
  );
}
