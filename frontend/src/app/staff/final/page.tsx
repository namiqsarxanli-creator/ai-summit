import type { Metadata } from "next";

import { FinalReview } from "@/components/competition/FinalReview";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Final result",
};

// Game Master (or administrator): snapshot, review and publish the final result.
export default function FinalReviewPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Final result" description="Review and publish for the Grand Final" kind="leaderboard" backHref="/staff" />
      <FinalReview />
      <BrandCredit />
    </main>
  );
}
