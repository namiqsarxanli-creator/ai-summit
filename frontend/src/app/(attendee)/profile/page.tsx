import type { Metadata } from "next";
import Link from "next/link";

import { ProfileView } from "@/components/profile/ProfileView";

export const metadata: Metadata = {
  title: "Profile · FACEMARK",
};

export default function ProfilePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col gap-8 px-4 py-10 pb-24 sm:px-6">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Profile
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Your profile</h1>
        <p className="text-sm text-slate-400">
          <Link href="/" className="font-medium text-indigo-300 hover:text-indigo-200">
            ← Home
          </Link>
        </p>
      </header>
      <ProfileView />
    </main>
  );
}
