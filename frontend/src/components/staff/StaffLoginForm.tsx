"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { TextField } from "@/components/ui/TextField";
import { messageFor } from "@/lib/errors";
import { staffLogin } from "@/lib/staff";

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// §12.1: email + password. No self-registration link anywhere on this
// form — staff accounts are seed/admin-created only (§12.1), and there
// is nowhere in the API surface this page could point to even if it
// wanted to.
export function StaffLoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setEmailError(null);
    setPasswordError(null);
    if (!EMAIL_SHAPE.test(email.trim())) {
      setEmailError("Enter your staff email address.");
      return;
    }
    if (password.length === 0) {
      setPasswordError("Enter your password.");
      return;
    }
    setBusy(true);
    const result = await staffLogin(email.trim(), password);
    setBusy(false);
    if (result.ok) {
      router.push("/staff");
      return;
    }
    // §12.1: one message regardless of cause — this form never guesses
    // which of "unknown email", "wrong password" or "locked" applies
    // beyond what the server's own code already says.
    setPassword("");
    setFormError(messageFor(result.error.code));
  }

  return (
    <form noValidate onSubmit={onSubmit} className="flex flex-col gap-6" aria-busy={busy}>
      <TextField
        id="staff-email"
        label="Email"
        type="email"
        autoComplete="username"
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        error={emailError ?? undefined}
      />
      <TextField
        id="staff-password"
        label="Password"
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={passwordError ?? undefined}
      />
      {formError && (
        <p
          role="alert"
          className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300"
        >
          {formError}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="rounded-lg bg-indigo-500 px-4 py-3 font-semibold text-white transition-colors hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
