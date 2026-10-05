"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import { fetchStaffMe } from "@/lib/staff";
import { changePassword, newPasswordProblem } from "@/lib/staffPassword";

type Phase = "checking" | "signed-out" | "form" | "done";

const ERROR_BOX = "fm-alert fm-alert-error";
const PRIMARY = "fm-btn fm-btn-primary fm-btn-block";

// A signed-in staff member or administrator changes their own password: the
// CURRENT password again, the new one twice. Nothing is claimed before the
// server answers; on success the server has ended every session (this one too),
// so the screen says so and offers the way back to sign-in.
export function StaffPasswordForm() {
  const { t } = useLanguage();
  const [phase, setPhase] = useState<Phase>("checking");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [currentError, setCurrentError] = useState<string | null>(null);
  const [newError, setNewError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [sessionEnded, setSessionEnded] = useState(false);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void fetchStaffMe().then((result) => {
      if (cancelled) return;
      setPhase(result.ok ? "form" : "signed-out");
    });
    return () => { cancelled = true; };
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return; // a double click is one request
    setFormError(null);
    setCurrentError(null);
    setNewError(null);
    setSessionEnded(false);
    if (current.length === 0) {
      setCurrentError("Enter your current password.");
      return;
    }
    const problem = newPasswordProblem(next, confirmation);
    if (problem) {
      setNewError(problem);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    const result = await changePassword(current, next, confirmation);
    inFlight.current = false;
    setBusy(false);
    if (result.ok) {
      setCurrent("");
      setNext("");
      setConfirmation("");
      setPhase("done");
      return;
    }
    const code = result.error.code;
    if (code === "CURRENT_PASSWORD_INVALID") setCurrent("");
    if (code === "UNAUTHENTICATED" || code === "STAFF_NOT_AUTHORIZED") setSessionEnded(true);
    setFormError(messageFor(code));
  }

  if (phase === "checking") {
    return <p className="fm-note">{t("Checking your session…")}</p>;
  }

  if (phase === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-small">{t("Sign in first to change your password.")}</p>
        <Link href="/staff/login" className={PRIMARY}>{t("Staff sign in")}</Link>
      </div>
    );
  }

  if (phase === "done") {
    return (
      <div className="fm-stack">
        <p role="status" className="fm-alert fm-alert-ok">
          <strong className="block">{t("Password changed")}</strong>
          {t("Your password was changed. Every session of your account has ended, on every device. Sign in again with the new password.")}
        </p>
        <Link href="/staff/login" className={PRIMARY}>{t("Sign in again")}</Link>
      </div>
    );
  }

  return (
    <form noValidate onSubmit={onSubmit} className="fm-form fm-auth-form" aria-busy={busy}>
      <TextField
        id="staff-current-password"
        label="Current password"
        type="password"
        autoComplete="current-password"
        value={current}
        onChange={(event) => setCurrent(event.target.value)}
        error={currentError ?? undefined}
      />
      <TextField
        id="staff-new-password"
        label="New password"
        type="password"
        autoComplete="new-password"
        hint="At least 12 characters."
        value={next}
        onChange={(event) => setNext(event.target.value)}
        error={newError ?? undefined}
      />
      <TextField
        id="staff-new-password-again"
        label="Repeat the new password"
        type="password"
        autoComplete="new-password"
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
      />
      {formError && (
        <div role="alert" className={ERROR_BOX}>
          <p>{t(formError)}</p>
          {formError === messageFor("NETWORK_ERROR") && (
            <p className="fm-small">{t("If the password was already changed, sign in with the new one.")}</p>
          )}
          {sessionEnded && <Link href="/staff/login" className="fm-link fm-link-block">{t("Sign in again")}</Link>}
        </div>
      )}
      <button type="submit" disabled={busy} className={PRIMARY}>
        {busy ? t("Changing password…") : t("Change password")}
      </button>
    </form>
  );
}
