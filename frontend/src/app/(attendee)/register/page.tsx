import type { Metadata } from "next";
import Link from "next/link";

import { RegisterForm } from "@/components/registration/RegisterForm";

export const metadata: Metadata = {
  title: "Register · FACEMARK",
};

export default function RegisterPage() {
  return (
    <main id="main-content" className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 px-6 py-12">
      <header className="flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-400">
          AI Summit · Attendee registration
        </p>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-50">
          Join the summit network
        </h1>
        <p className="text-sm text-slate-400">
          Register once to get your networking card and personalised booth recommendations.
          We&apos;ll email you a code to confirm it&apos;s really you.
        </p>
        <p className="text-sm text-slate-400">
          Already registered?{" "}
          <Link href="/login" className="font-medium text-indigo-300 hover:text-indigo-200">
            Sign in
          </Link>
        </p>
      </header>
      <RegisterForm />
    </main>
  );
}
