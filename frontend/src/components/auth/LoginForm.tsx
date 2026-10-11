"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ChangeEvent, type FormEvent, useState } from "react";

import { isSixDigitCode } from "@/components/registration/validation";
import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { messageFor } from "@/lib/errors";

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// Mirrors the server's non-enumerating copy (§8.2): the UI never claims
// to know whether the address is registered.
const CODE_SENT = "If this email is registered, we've sent a 6-digit sign-in code.";

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
  const [busy, setBusy] = useState(false);

  async function requestCode(): Promise<boolean> {
    setBusy(true);
    const result = await apiPost<{ message: string }>("/api/auth/otp/request", {
      email: email.trim(),
      language,
    });
    setBusy(false);
    if (!result.ok) {
      setFormError(messageFor(result.error.code));
      return false;
    }
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
      router.push("/");
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
      <label className="fm-remember"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} />{t("Remember me on this device for 30 days")}</label>
      {banners}
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
