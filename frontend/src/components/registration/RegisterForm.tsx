"use client";

import { useRouter } from "next/navigation";
import { type ChangeEvent, type FormEvent, useRef, useState } from "react";

import { InterestCardFields } from "@/components/profile/InterestCardFields";
import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { fieldErrorsFrom, messageFor } from "@/lib/errors";
import { EMPTY_CARD, type InterestCard } from "@/lib/interestCard";
import { savePendingEmail } from "@/lib/pendingEmail";

import {
  type ConsentChoice,
  type RegistrationValues,
  validateRegistration,
} from "./validation";

const EMPTY: RegistrationValues = {
  first_name: "",
  last_name: "",
  email: "",
  phone: "",
  profession: "",
  linkedin_url: "",
  company: "",
};

export function RegisterForm() {
  const { t, language } = useLanguage();
  const router = useRouter();
  const [values, setValues] = useState<RegistrationValues>(EMPTY);
  const [consent, setConsent] = useState<ConsentChoice>(null);
  const [card, setCard] = useState<InterestCard>(EMPTY_CARD);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);

  function update(field: keyof RegistrationValues) {
    return (event: ChangeEvent<HTMLInputElement>) =>
      setValues((current) => ({ ...current, [field]: event.target.value }));
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    setFormError(null);

    const clientErrors = validateRegistration(values, consent, card);
    setFieldErrors(clientErrors);
    if (Object.keys(clientErrors).length > 0) return;

    const email = values.email.trim();
    const linkedin = values.linkedin_url.trim();
    const company = values.company.trim();
    inFlight.current = true;
    setSubmitting(true);
    const result = await apiPost<{ message: string }>("/api/attendees/register", {
      first_name: values.first_name.trim(),
      last_name: values.last_name.trim(),
      email,
      phone: values.phone.trim(),
      profession: values.profession.trim(),
      ...(linkedin ? { linkedin_url: linkedin } : {}),
      company: company || null,
      interest_card: card,
      // Booth-recommendation interests are chosen later, on the profile page.
      interest_slugs: [],
      sponsor_consent: consent === "yes",
      language,
    });
    inFlight.current = false;
    setSubmitting(false);

    if (result.ok) {
      savePendingEmail(email);
      router.push("/verify");
      return;
    }
    if (result.error.code === "VALIDATION_ERROR") {
      setFieldErrors(fieldErrorsFrom(result.error.details));
    }
    setFormError(messageFor(result.error.code));
  }

  return (
    <form noValidate onSubmit={onSubmit} className="fm-form fm-auth-form" aria-busy={submitting}>
      <section className="fm-grid-2">
        <TextField
          id="first_name"
          label="First name"
          autoComplete="given-name"
          value={values.first_name}
          onChange={update("first_name")}
          error={fieldErrors.first_name}
          required
        />
        <TextField
          id="last_name"
          label="Last name"
          autoComplete="family-name"
          value={values.last_name}
          onChange={update("last_name")}
          error={fieldErrors.last_name}
          required
        />
        <TextField
          id="email"
          label="Email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={update("email")}
          error={fieldErrors.email}
          hint="We'll send a 6-digit verification code here."
          required
        />
        <TextField
          id="phone"
          label="Phone"
          type="tel"
          autoComplete="tel"
          value={values.phone}
          onChange={update("phone")}
          error={fieldErrors.phone}
          hint="Never shown on your public networking card."
          required
        />
        <TextField
          id="profession"
          label="Profession / role"
          autoComplete="organization-title"
          value={values.profession}
          onChange={update("profession")}
          error={fieldErrors.profession}
          required
        />
        <TextField
          id="company"
          label="Company"
          autoComplete="organization"
          maxLength={200}
          hint="No company? Write Student or Self-employed."
          required
          value={values.company}
          onChange={update("company")}
          error={fieldErrors.company}
        />
        <TextField
          id="linkedin_url"
          label="LinkedIn (optional)"
          type="url"
          inputMode="url"
          placeholder="https://www.linkedin.com/in/your-name"
          value={values.linkedin_url}
          onChange={update("linkedin_url")}
          error={fieldErrors.linkedin_url}
        />
      </section>

      <InterestCardFields
        idPrefix="register"
        card={card}
        onChange={(next) => {
          setCard(next);
          setFieldErrors((current) => ({ ...current, interest_card: "" }));
        }}
        error={fieldErrors.interest_card || undefined}
      />

      <fieldset
        className="fm-fieldset fm-card fm-card-tint"
        aria-describedby={fieldErrors.sponsor_consent ? "sponsor_consent-error" : undefined}
      >
        <legend className="fm-legend">{t("Sponsor data sharing")}</legend>
        <p className="fm-hint">
          {t("Separate from registering, and entirely your choice: may we share your registration details with the event's sponsors? You can register either way.")}
        </p>
        <label className="fm-option">
          <input
            type="radio"
            name="sponsor_consent"
            value="yes"
            checked={consent === "yes"}
            onChange={() => setConsent("yes")}
          />
          {t("Yes, share my details with sponsors")}
        </label>
        <label className="fm-option">
          <input
            type="radio"
            name="sponsor_consent"
            value="no"
            checked={consent === "no"}
            onChange={() => setConsent("no")}
          />
          {t("No, don't share my details")}
        </label>
        {fieldErrors.sponsor_consent && (
          <p id="sponsor_consent-error" role="alert" className="fm-error">
            {t(fieldErrors.sponsor_consent)}
          </p>
        )}
      </fieldset>

      {formError && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(formError)}
        </p>
      )}

      <button type="submit" disabled={submitting} className="fm-btn fm-btn-primary fm-btn-block">
        {submitting ? t("Registering…") : t("Register and get my code")}
      </button>
    </form>
  );
}
