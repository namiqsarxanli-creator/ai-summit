import type { Metadata } from "next";

import { StaffLoginForm } from "@/components/staff/StaffLoginForm";

export const metadata: Metadata = {
  title: "Staff sign in · FACEMARK",
};

// M09: staff sign-in, kept entirely separate from the attendee
// experience (different route tree, different session cookie
// contents, different endpoints) — a summit visitor never lands here
// by accident, and a staff member never sees attendee-only UI.
export default function StaffLoginPage() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-8 px-6 py-12">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Staff
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Staff sign in</h1>
        <p className="text-sm text-slate-400">
          Accounts are created by an admin — there is no staff self-registration.
        </p>
      </header>
      <StaffLoginForm />
    </main>
  );
}
