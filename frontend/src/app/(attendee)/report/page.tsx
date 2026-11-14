import type { Metadata } from "next";

import { MyReport } from "@/components/competition/MyReport";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "My report",
};

// After the event (13-14 November): one's own results, history and connections.
export default function ReportPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="My report" description="Your AI Summit day in one place" kind="leaderboard" />
      <MyReport />
      <BrandCredit />
    </main>
  );
}
