import Link from "next/link";

import { BoothDetailView } from "@/components/matching/BoothDetailView";

export default async function BoothDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 px-6 py-12 pb-24">
      <p className="text-sm text-slate-400">
        <Link href="/booths" className="font-medium text-indigo-300 hover:text-indigo-200">
          ← Back to booths
        </Link>
      </p>
      <BoothDetailView slug={slug} />
    </main>
  );
}
