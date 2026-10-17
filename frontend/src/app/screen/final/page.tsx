import type { Metadata } from "next";

import { FinalScreen } from "@/components/competition/FinalScreen";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Grand Final",
  robots: { index: false, follow: false },
};

// For the Grand Final screen (17:40). Public and privacy-safe: only what a Game
// Master has published, as first name and surname initial.
export default function FinalScreenPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Grand Final" description="AI Summit · Beyond Now" kind="leaderboard" showBack={false} />
      <FinalScreen />
      <BrandCredit />
    </main>
  );
}
