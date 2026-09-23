import type { Metadata } from "next";
import Link from "next/link";

import { MyCard } from "@/components/card/MyCard";

export const metadata: Metadata = {
  title: "My card · FACEMARK",
};

export default function MyCardPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col gap-8 px-4 py-10 pb-24 sm:px-6">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · My card
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Your networking QR</h1>
        <p className="text-sm text-slate-400">
          <Link href="/" className="font-medium text-indigo-300 hover:text-indigo-200">
            ← Home
          </Link>
        </p>
      </header>
      <MyCard />
    </main>
  );
}
