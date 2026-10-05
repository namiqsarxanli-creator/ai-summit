import type { Metadata } from "next";

import { PublicCardView } from "@/components/card/PublicCardView";
import { BrandCredit } from "@/components/ui/BrandCredit";
import { T } from "@/components/ui/LanguageProvider";

// §21.11 / §9.5: the path of this page IS the card token. Besides the
// response headers set in next.config.ts, the document itself says
// no-referrer (so tapping LinkedIn does not hand over the URL) and
// noindex. The title names no one.
export const metadata: Metadata = {
  title: "Networking card",
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default async function PublicCardPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return (
    <main id="main-content" className="fm-page" style={{ paddingTop: 12 }}>
      <p className="fm-eyebrow">
        <T id="AI Summit · Networking card" />
      </p>
      <PublicCardView token={token} />
      <BrandCredit />
    </main>
  );
}
