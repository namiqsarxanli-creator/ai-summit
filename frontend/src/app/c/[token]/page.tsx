import type { Metadata } from "next";

import { PublicCardView } from "@/components/card/PublicCardView";

// §21.11 / §9.5: the path of this page IS the card token. Besides the
// response headers set in next.config.ts, the document itself says
// no-referrer (so tapping LinkedIn does not hand over the URL) and
// noindex. The title names no one.
export const metadata: Metadata = {
  title: "Networking card · FACEMARK",
  referrer: "no-referrer",
  robots: { index: false, follow: false },
};

export default async function PublicCardPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col gap-6 px-4 py-10 sm:px-6">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
        AI Summit · Networking card
      </p>
      <PublicCardView token={token} />
    </main>
  );
}
