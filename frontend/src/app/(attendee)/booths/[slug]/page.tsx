import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { BrandCredit } from "@/components/ui/BrandCredit";
import Link from "next/link";

import { BoothDetailView } from "@/components/matching/BoothDetailView";

export default async function BoothDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return (
    <main id="main-content" className="fm-page">
      <div className="fm-page-header">
        <div className="fm-page-topline">
          <span />
          <Link href="/" className="fm-wordmark">AI Summit</Link>
        </div>
      </div>
      <BoothDetailView slug={slug} />
      <AttendeeTabBar active="booths" />
      <BrandCredit />
    </main>
  );
}
