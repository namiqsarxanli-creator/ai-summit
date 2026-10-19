"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { TextField } from "@/components/ui/TextField";
import { messageFor } from "@/lib/errors";
import { staffLogin } from "@/lib/staff";

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// Where a sign-in may return to (`?next=`): staff pages of this site only, named
// exactly - never a full URL, another site or an arbitrary path. Anything else,
// or nothing, goes to the staff page as before.
const RETURN_TO = new Set(["/staff/organiser", "/staff/attendance", "/staff/final", "/staff/playground", "/staff/scan", "/staff/booth-report"]);

export function returnPath(search: string): string {
  const next = new URLSearchParams(search).get("next");
  return next !== null && RETURN_TO.has(next) ? next : "/staff";
}

// §12.1: email + password. No self-registration link anywhere on this
// form — staff accounts are seed/admin-created only (§12.1), and there
// is nowhere in the API surface this page could point to even if it
// wanted to.
export function StaffLoginForm() {
  const { t } = useLanguage();
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
      router.push(returnPath(window.location.search));
      return;
    }
    // §12.1: one message regardless of cause — this form never guesses
    // which of "unknown email", "wrong password" or "locked" applies
    // beyond what the server's own code already says.
    setPassword("");
    setFormError(messageFor(result.error.code));
  }

  return (
    <form noValidate onSubmit={onSubmit} className="fm-form fm-auth-form" aria-busy={busy}>
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
        <p role="alert" className="fm-alert fm-alert-error">
          {t(formError)}
        </p>
      )}
      <button
        type="submit"
        disabled={busy}
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {busy ? t("Signing in…") : t("Sign in")}
      </button>
      <Link href="/staff/reset" className="fm-btn fm-btn-quiet fm-btn-block">
        {t("Forgot your password?")}
      </Link>
    </form>
  );
}
