"use client";

import Link from "next/link";
import { type ChangeEvent, type FormEvent, useRef, useState, useSyncExternalStore } from "react";

import { TextField } from "@/components/ui/TextField";
import { apiPost } from "@/lib/api";
import { fieldErrorsFrom, messageFor } from "@/lib/errors";
import { clearPendingEmail, readPendingEmail } from "@/lib/pendingEmail";

import { isSixDigitCode } from "./validation";

// sessionStorage never changes underneath this page, so there is
// nothing to subscribe to — useSyncExternalStore is used only for its
// SSR-safe read (empty on the server, the stored value on the client).
const noSubscription = () => () => {};

const RESEND_NOTICE = "If this email is registered, a new code has been sent.";

export function VerifyForm() {
  const storedEmail = useSyncExternalStore(noSubscription, readPendingEmail, () => "");
  const [emailOverride, setEmailOverride] = useState<string | null>(null);
  const email = emailOverride ?? storedEmail;

  const [code, setCode] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [resending, setResending] = useState(false);
  const [activated, setActivated] = useState(false);
  const inFlight = useRef(false);

  if (activated) {
    return (
      <div
        role="status"
        className="flex flex-col items-center gap-4 rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-8 text-center"
      >
        <div
          aria-hidden
          className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500 text-2xl text-slate-950"
        >
          ✓
        </div>
        <h2 className="text-xl font-semibold text-emerald-100">You&apos;re verified</h2>
        <p className="text-sm text-emerald-200/80">
          Your attendee account is now active and you&apos;re signed in on this device.
        </p>
        <Link href="/" className="text-sm font-medium text-indigo-300 hover:text-indigo-200">
          Back to home
        </Link>
      </div>
    );
  }

  function onCodeChange(event: ChangeEvent<HTMLInputElement>) {
    setCode(event.target.value.replace(/\D/g, "").slice(0, 6));
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    setFormError(null);
    setNotice(null);

    const errors: Record<string, string> = {};
    if (!email.trim()) errors.email = "Enter the email you registered with.";
    if (!isSixDigitCode(code)) errors.code = "Enter the 6-digit code from your email.";
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    inFlight.current = true;
    setSubmitting(true);
    const result = await apiPost<{ status: string }>("/api/auth/otp/verify", {
      email: email.trim(),
      code,
    });
    inFlight.current = false;
    setSubmitting(false);

    if (result.ok) {
      clearPendingEmail();
      setActivated(true);
      return;
    }
    if (result.error.code === "VALIDATION_ERROR") {
      setFieldErrors(fieldErrorsFrom(result.error.details));
    }
    if (result.error.code === "OTP_INVALID") {
      setCode("");
    }
    setFormError(messageFor(result.error.code));
  }

  async function onResend() {
    if (inFlight.current) return;
    setFormError(null);
    setNotice(null);
    if (!email.trim()) {
      setFieldErrors({ email: "Enter the email you registered with." });
      return;
    }
    inFlight.current = true;
    setResending(true);
    const result = await apiPost<{ message: string }>("/api/auth/otp/request", {
      email: email.trim(),
    });
    inFlight.current = false;
    setResending(false);
    if (result.ok) {
      setCode("");
      setNotice(RESEND_NOTICE);
    } else {
      setFormError(messageFor(result.error.code));
    }
  }

  return (
    <form noValidate onSubmit={onSubmit} className="flex flex-col gap-6" aria-busy={submitting}>
      <TextField
        id="email"
        label="Email"
        type="email"
        autoComplete="email"
        value={email}
        onChange={(event) => setEmailOverride(event.target.value)}
        error={fieldErrors.email}
      />
      <TextField
        id="code"
        label="Verification code"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder="000000"
        maxLength={6}
        value={code}
        onChange={onCodeChange}
        error={fieldErrors.code}
        hint="The 6-digit code expires 10 minutes after it was sent."
        className="text-center font-mono text-2xl tracking-[0.5em]"
      />

      {notice && (
        <p role="status" className="rounded-lg border border-indigo-500/40 bg-indigo-500/10 p-3 text-sm text-indigo-200">
          {notice}
        </p>
      )}
      {formError && (
        <p role="alert" className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">
          {formError}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="rounded-lg bg-indigo-500 px-4 py-3 font-semibold text-white transition-colors hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {submitting ? "Verifying…" : "Verify and activate"}
      </button>
      <button
        type="button"
        onClick={onResend}
        disabled={resending}
        className="text-sm font-medium text-indigo-300 hover:text-indigo-200 disabled:opacity-60"
      >
        {resending ? "Sending…" : "Send me a new code"}
      </button>
    </form>
  );
}
