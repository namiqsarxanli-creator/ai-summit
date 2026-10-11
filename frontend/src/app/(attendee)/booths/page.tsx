import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";
import type { Metadata } from "next";

import { Recommendations } from "@/components/matching/Recommendations";

export const metadata: Metadata = {
  title: "Booth matches",
};

export default function BoothsPage() {
  return (
    <main id="main-content" className="fm-page fm-screen-booths">
      <PageHeader showBack={false} title="Booths" description="Discover booth teams" kind="booths" />
      <Recommendations />
      <BrandCredit />
    </main>
  );
}
