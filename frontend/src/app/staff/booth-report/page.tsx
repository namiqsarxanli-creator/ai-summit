import type { Metadata } from "next";

import { BoothReport } from "@/components/staff/BoothReport";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Booth report",
};

export default function BoothReportPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Booth report" description="Your booth only" kind="leaderboard" backHref="/staff" />
      <BoothReport />
      <BrandCredit />
    </main>
  );
}
