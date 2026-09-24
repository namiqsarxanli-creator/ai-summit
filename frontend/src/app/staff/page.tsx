import type { Metadata } from "next";

import { StaffPanel } from "@/components/staff/StaffPanel";

export const metadata: Metadata = {
  title: "Staff panel · FACEMARK",
};

export default function StaffPage() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-md flex-col gap-8 px-4 py-10 sm:px-6">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Staff panel
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Your booth</h1>
      </header>
      <StaffPanel />
    </main>
  );
}
