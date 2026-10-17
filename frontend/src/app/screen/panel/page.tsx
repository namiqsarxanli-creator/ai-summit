import type { Metadata } from "next";

import { PanelScreen } from "@/components/competition/PanelScreen";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Panel codes",
  robots: { index: false, follow: false },
};

// For the Main Stage screen; the data (and the event's time zone) comes from the
// admin API, so it needs an administrator session.
export default function PanelScreenPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Panel codes" description="Scan with AI Summit" kind="leaderboard" showBack={false} />
      <PanelScreen />
      <BrandCredit />
    </main>
  );
}
