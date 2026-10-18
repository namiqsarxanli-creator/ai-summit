import type { Metadata } from "next";

import { LiveScreen } from "@/components/competition/LiveScreen";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Live screen",
  robots: { index: false, follow: false },
};

// The venue's big screen during the day: live top 10, latest points, the
// countdown to the 17:30 close and the shared photos; after the close the
// review, then the published final. Public and privacy-safe: first name and
// surname initial only, no contact data.
export default function LiveScreenPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-wide">
      <PageHeader title="Beyond Now" description="AI Summit · Beyond Now" kind="leaderboard" showBack={false} />
      <LiveScreen />
      <BrandCredit />
    </main>
  );
}
