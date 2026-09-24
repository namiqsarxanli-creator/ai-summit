import type { Metadata } from "next";

import { LoginForm } from "@/components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Sign in · FACEMARK",
};

export default function LoginPage() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-8 px-6 py-12">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Welcome back
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">Sign in</h1>
        <p className="text-sm text-slate-400">
          No password needed — we&apos;ll email you a one-time code.
        </p>
      </header>
      <LoginForm />
    </main>
  );
}
