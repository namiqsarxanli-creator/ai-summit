import type { Metadata } from "next";

import { PhotoFeed } from "@/components/people/PhotoFeed";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { PageHeader } from "@/components/ui/PageHeader";

export const metadata: Metadata = {
  title: "Beyond Now Photo",
  robots: { index: false, follow: false },
};

// For the screen next to the ranking: approved pair photos only.
export default function PhotoScreenPage() {
  return (
    <main id="main-content" className="fm-page">
      <PageHeader title="Beyond Now Photo" description="AI Summit · Connection Hub" kind="leaderboard" showBack={false} />
      <PhotoFeed />
      <BrandCredit />
    </main>
  );
}
