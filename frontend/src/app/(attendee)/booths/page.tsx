import type { Metadata } from "next";
import Link from "next/link";

import { Recommendations } from "@/components/matching/Recommendations";

export const metadata: Metadata = {
  title: "Booth matches · FACEMARK",
};

export default function BoothsPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 px-6 py-12">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Booths
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Your booth matches</h1>
        <p className="text-sm text-slate-400">
          A deterministic score computed from your selected interests — never randomised, never
          generated.
        </p>
        <p className="text-sm text-slate-400">
          <Link href="/" className="font-medium text-indigo-300 hover:text-indigo-200">
            ← Home
          </Link>
        </p>
      </header>
      <Recommendations />
    </main>
  );
}
