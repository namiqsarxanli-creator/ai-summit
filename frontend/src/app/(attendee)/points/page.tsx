import type { Metadata } from "next";

import { PanelScan } from "@/components/competition/PanelScan";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "My points",
};

// Reached from Home: own competition points and the Main Stage panel scan.
export default function PointsPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="My points" description="Verified participation, up to 100 points" kind="leaderboard" />
      <PanelScan />
      <BrandCredit />
    </main>
  );
}
