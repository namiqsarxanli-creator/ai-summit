"use client";

import Link from "next/link";
import { type FormEvent, useRef, useState } from "react";

import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import { codeProblem, confirmPasswordReset, newPasswordProblem, requestPasswordReset } from "@/lib/staffPassword";

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const ERROR_BOX = "fm-alert fm-alert-error";
const PRIMARY = "fm-btn fm-btn-primary fm-btn-block";
const SECONDARY = "fm-btn fm-btn-secondary fm-btn-block";

type Step = "email" | "code" | "done";

// "I forgot my password": ask for a one-time code by email, then give the code
// and a new password (twice). The same message is shown whatever the address is -
// this screen never says whether it belongs to an account. Success ends every
// session and signs nobody in: the person signs in with the new password.
export function StaffResetForm() {
  const { t, language } = useLanguage();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [emailError, setEmailError] = useState<string | null>(null);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [newError, setNewError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  function clearErrors() {
    setEmailError(null);
    setCodeError(null);
    setNewError(null);
    setFormError(null);
  }

  function emailIsValid(): boolean {
    if (EMAIL_SHAPE.test(email.trim())) return true;
    setEmailError("Enter your staff email address.");
    return false;
  }

  async function onRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    clearErrors();
    if (!emailIsValid()) return;
    inFlight.current = true;
    setBusy(true);
    const result = await requestPasswordReset(email.trim(), language);
    inFlight.current = false;
    setBusy(false);
    if (result.ok) {
      setSent(true);
      setStep("code");
      return;
    }
    setFormError(messageFor(result.error.code));
  }

  async function onConfirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    clearErrors();
    const wrongCode = codeProblem(code);
    if (wrongCode) {
      setCodeError(wrongCode);
      return;
    }
    const problem = newPasswordProblem(next, confirmation);
    if (problem) {
      setNewError(problem);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    const result = await confirmPasswordReset(email.trim(), code.trim(), next, confirmation);
    inFlight.current = false;
    setBusy(false);
    if (result.ok) {
      setCode("");
      setNext("");
      setConfirmation("");
      setStep("done");
      return;
    }
    if (result.error.code === "OTP_INVALID") setCode("");
    setFormError(messageFor(result.error.code));
  }

  if (step === "done") {
    return (
      <div className="fm-stack">
        <p role="status" className="fm-alert fm-alert-ok">
          <strong className="block">{t("Password reset")}</strong>
          {t("Your password was reset. Sign in with the new password.")}
        </p>
        <Link href="/staff/login" className={PRIMARY}>{t("Back to sign in")}</Link>
      </div>
    );
  }

  const error = formError && (
    <div role="alert" className={ERROR_BOX}>
      <p>{t(formError)}</p>
      {formError === messageFor("NETWORK_ERROR") && step === "code" && (
        <p className="fm-small">{t("If the password was already changed, sign in with the new one.")}</p>
      )}
    </div>
  );

  if (step === "email") {
    return (
      <form noValidate onSubmit={onRequest} className="fm-form fm-auth-form" aria-busy={busy}>
        <TextField
          id="staff-reset-email"
          label="Staff email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          error={emailError ?? undefined}
        />
        {error}
        <button type="submit" disabled={busy} className={PRIMARY}>
          {busy ? t("Sending…") : t("Send the code")}
        </button>
        <button type="button" className={SECONDARY} disabled={busy} onClick={() => { clearErrors(); if (emailIsValid()) setStep("code"); }}>
          {t("I already have a code")}
        </button>
        <Link href="/staff/login" className="fm-btn fm-btn-quiet fm-btn-block">{t("Back to sign in")}</Link>
      </form>
    );
  }

  return (
    <form noValidate onSubmit={onConfirm} className="fm-form fm-auth-form" aria-busy={busy}>
      {sent && (
        <p role="status" className="fm-alert fm-alert-info">
          {t("If this address belongs to an active staff account, a code has been sent to it. It is valid for 10 minutes.")}
        </p>
      )}
      <TextField
        id="staff-reset-code"
        label="Code from the email"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        error={codeError ?? undefined}
      />
      <TextField
        id="staff-reset-new-password"
        label="New password"
        type="password"
        autoComplete="new-password"
        hint="At least 12 characters."
        value={next}
        onChange={(event) => setNext(event.target.value)}
        error={newError ?? undefined}
      />
      <TextField
        id="staff-reset-new-password-again"
        label="Repeat the new password"
        type="password"
        autoComplete="new-password"
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
      />
      {error}
      <button type="submit" disabled={busy} className={PRIMARY}>
        {busy ? t("Changing password…") : t("Set the new password")}
      </button>
      <button type="button" className={SECONDARY} disabled={busy} onClick={() => { clearErrors(); setStep("email"); }}>
        {t("Send a new code")}
      </button>
      <p className="fm-small fm-center">{t("Staff email")}: {email.trim()}</p>
    </form>
  );
}
