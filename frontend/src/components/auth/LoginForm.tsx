"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ChangeEvent, type FormEvent, useState } from "react";

import { isSixDigitCode } from "@/components/registration/validation";
import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { hasPendingPanelCode } from "@/lib/pendingPanelCode";

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// An address with no account is told so by the server (EMAIL_NOT_REGISTERED,
// 10 Nov 2026) and pointed to registration instead of waiting for a code.
const CODE_SENT = "We've sent a 6-digit sign-in code to your email.";

type Step = "email" | "code";

export function LoginForm() {
  const { t, language } = useLanguage();
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [remember, setRemember] = useState(false);
  const [code, setCode] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [notRegistered, setNotRegistered] = useState(false);
  const [busy, setBusy] = useState(false);

  async function requestCode(): Promise<boolean> {
    setBusy(true);
    const result = await apiPost<{ message: string }>("/api/auth/otp/request", {
      email: email.trim(),
      language,
    });
    setBusy(false);
    if (!result.ok) {
      setNotRegistered(result.error.code === "EMAIL_NOT_REGISTERED");
      setFormError(messageFor(result.error.code));
      return false;
    }
    setNotRegistered(false);
    setNotice(CODE_SENT);
    return true;
  }

  async function onRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setNotice(null);
    if (!EMAIL_SHAPE.test(email.trim())) {
      setFieldError("Enter the email you registered with.");
      return;
    }
    setFieldError(null);
    if (await requestCode()) setStep("code");
  }

  async function onVerify(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (!isSixDigitCode(code)) {
      setFieldError("Enter the 6-digit code from your email.");
      return;
    }
    setFieldError(null);
    setBusy(true);
    const result = await apiPost<{ status: string }>("/api/auth/otp/verify", {
      email: email.trim(),
      code,
      ...(remember ? { remember_device: true } : {}),
    });
    setBusy(false);
    if (result.ok) {
      // A panel code scanned before signing in goes on to My points, which sends it.
      router.push(hasPendingPanelCode() ? "/points" : "/");
      return;
    }
    if (result.error.code === "OTP_INVALID") setCode("");
    setNotice(null);
    setFormError(messageFor(result.error.code));
  }

  async function onResend() {
    setFormError(null);
    setNotice(null);
    setCode("");
    await requestCode();
  }

  const banners = (
    <>
      {notice && (
        <p role="status" className="fm-alert fm-alert-info">
          {t(notice)}
        </p>
      )}
      {formError && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(formError)}
          {notRegistered && (
            <>
              {" "}
              <Link href="/register" className="fm-link">{t("Register as an attendee")}</Link>
            </>
          )}
        </p>
      )}
    </>
  );

  if (step === "email") {
    return (
      <form noValidate onSubmit={onRequest} className="fm-form fm-auth-form" aria-busy={busy}>
        <TextField
          id="email"
          label="Email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          error={fieldError ?? undefined}
        />
        {banners}
        <button
          type="submit"
          disabled={busy}
          className="fm-btn fm-btn-primary fm-btn-block"
        >
          {busy ? t("Sending…") : t("Send me a sign-in code")}
        </button>
        <p className="fm-small fm-center">{t("New here?")}</p>
        <Link href="/register" className="fm-btn fm-btn-secondary fm-btn-block">
          {t("Register as an attendee")}
        </Link>
      </form>
    );
  }

  return (
    <form noValidate onSubmit={onVerify} className="fm-form fm-auth-form" aria-busy={busy}>
      <p className="fm-small">
        {t("Signing in as")} <span className="fm-strong">{email.trim()}</span>
      </p>
      <TextField
        id="code"
        label="Sign-in code"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder="000000"
        maxLength={6}
        value={code}
        onChange={(event: ChangeEvent<HTMLInputElement>) =>
          setCode(event.target.value.replace(/\D/g, "").slice(0, 6))
        }
        error={fieldError ?? undefined}
        hint="The code expires 10 minutes after it was sent."
        className="fm-input-code"
      />
      <label className="fm-remember"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} />{t("Remember me")}</label>
      {banners}
      {/* People without an account wait for a code that never comes (7 Nov 2026).
          The help says what to check without saying whether this address is registered. */}
      <div className="fm-small fm-muted">
        <p>{t("No code after 1–2 minutes? Check your spam or Promotions folder and that the email is spelled correctly.")}</p>
        <p>
          {t("No account with this email yet? Register first:")}{" "}
          <Link href="/register" className="fm-link">{t("Register as an attendee")}</Link>
        </p>
      </div>
      <button
        type="submit"
        disabled={busy}
        className="fm-btn fm-btn-primary fm-btn-block"
      >
        {busy ? t("Signing in…") : t("Sign in")}
      </button>
      <div className="fm-row-between">
        <button
          type="button"
          onClick={() => {
            setStep("email");
            setCode("");
            setNotice(null);
            setFormError(null);
            setFieldError(null);
          }}
          className="fm-btn fm-btn-quiet"
        >
          {t("Use a different email")}
        </button>
        <button
          type="button"
          onClick={onResend}
          disabled={busy}
          className="fm-btn fm-btn-quiet"
        >
          {t("Send a new code")}
        </button>
      </div>
    </form>
  );
}
