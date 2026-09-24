import type { Metadata } from "next";
import Link from "next/link";

import { Scanner } from "@/components/staff/Scanner";

export const metadata: Metadata = {
  title: "Scan attendee · FACEMARK",
};

export default function StaffScanPage() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-md flex-col gap-6 px-4 py-6 sm:px-6">
      <header className="flex items-center justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
            AI Summit · Staff
          </p>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-50">Scan attendee</h1>
        </div>
        <Link href="/staff" className="text-sm text-slate-400 underline">
          Back
        </Link>
      </header>
      <Scanner />
    </main>
  );
}
