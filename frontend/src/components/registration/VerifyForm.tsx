"use client";

import Link from "next/link";
import { type ChangeEvent, type FormEvent, useRef, useState, useSyncExternalStore } from "react";

import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
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
  const { t, language } = useLanguage();
  const storedEmail = useSyncExternalStore(noSubscription, readPendingEmail, () => "");
  const [emailOverride, setEmailOverride] = useState<string | null>(null);
  const email = emailOverride ?? storedEmail;

  const [remember, setRemember] = useState(false);
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
      <div role="status" className="fm-card fm-center fm-stack">
        <div aria-hidden className="fm-success-mark">
          ✓
        </div>
        <h2 className="fm-h2">{t("You're verified")}</h2>
        <p className="fm-text-soft">
          {t("Your attendee account is now active and you're signed in on this device.")}
        </p>
        <Link href="/" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Back to home")}
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
      ...(remember ? { remember_device: true } : {}),
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
      language,
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
    <form noValidate onSubmit={onSubmit} className="fm-form fm-auth-form" aria-busy={submitting}>
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
        className="fm-input-code"
      />

      <label className="fm-remember"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} />{t("Remember me")}</label>
      {notice && (
        <p role="status" className="fm-alert fm-alert-info">
          {t(notice)}
        </p>
      )}
      {formError && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(formError)}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {submitting ? t("Verifying…") : t("Verify and activate")}
      </button>
      <button
        type="button"
        onClick={onResend}
        disabled={resending}
        className="fm-btn fm-btn-quiet fm-btn-block"
      >
        {resending ? t("Sending…") : t("Send me a new code")}
      </button>
    </form>
  );
}
