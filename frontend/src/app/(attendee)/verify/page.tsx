import type { Metadata } from "next";

import { VerifyForm } from "@/components/registration/VerifyForm";

export const metadata: Metadata = {
  title: "Verify your email · FACEMARK",
};

export default function VerifyPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-8 px-6 py-12">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Step 2 of 2
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Check your email</h1>
        <p className="text-sm text-slate-400">
          Enter the 6-digit code we sent to activate your attendee account.
        </p>
      </header>
      <VerifyForm />
    </main>
  );
}
