"use client";

import { useRouter } from "next/navigation";
import { type ChangeEvent, type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { TextField } from "@/components/ui/TextField";
import { apiGet, apiPost } from "@/lib/api";
import { fieldErrorsFrom, messageFor } from "@/lib/errors";
import { savePendingEmail } from "@/lib/pendingEmail";

import {
  type ConsentChoice,
  type RegistrationValues,
  validateRegistration,
} from "./validation";

type Interest = { slug: string; name: string };

type Catalogue =
  | { kind: "loading" }
  | { kind: "ready"; interests: Interest[] }
  | { kind: "error" };

const EMPTY: RegistrationValues = {
  first_name: "",
  last_name: "",
  email: "",
  phone: "",
  profession: "",
  linkedin_url: "",
};

export function RegisterForm() {
  const router = useRouter();
  const [values, setValues] = useState<RegistrationValues>(EMPTY);
  const [selected, setSelected] = useState<string[]>([]);
  const [consent, setConsent] = useState<ConsentChoice>(null);
  const [catalogue, setCatalogue] = useState<Catalogue>({ kind: "loading" });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);

  const loadInterests = useCallback(() => {
    let cancelled = false;
    apiGet<Interest[]>("/api/interests").then((result) => {
      if (cancelled) return;
      setCatalogue(result.ok ? { kind: "ready", interests: result.data } : { kind: "error" });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => loadInterests(), [loadInterests]);

  function update(field: keyof RegistrationValues) {
    return (event: ChangeEvent<HTMLInputElement>) =>
      setValues((current) => ({ ...current, [field]: event.target.value }));
  }

  function toggleInterest(slug: string) {
    setSelected((current) =>
      current.includes(slug) ? current.filter((s) => s !== slug) : [...current, slug],
    );
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    setFormError(null);

    const clientErrors = validateRegistration(values, consent);
    setFieldErrors(clientErrors);
    if (Object.keys(clientErrors).length > 0) return;

    const email = values.email.trim();
    const linkedin = values.linkedin_url.trim();
    inFlight.current = true;
    setSubmitting(true);
    const result = await apiPost<{ message: string }>("/api/attendees/register", {
      first_name: values.first_name.trim(),
      last_name: values.last_name.trim(),
      email,
      phone: values.phone.trim(),
      profession: values.profession.trim(),
      ...(linkedin ? { linkedin_url: linkedin } : {}),
      interest_slugs: selected,
      sponsor_consent: consent === "yes",
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
    <form noValidate onSubmit={onSubmit} className="flex flex-col gap-8" aria-busy={submitting}>
      <section className="grid gap-4 sm:grid-cols-2">
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
          label="Work email"
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

      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium text-slate-200">Your interests</legend>
        <p className="text-xs text-slate-500">
          Pick the topics you care about — they shape your booth recommendations.
        </p>
        {catalogue.kind === "loading" && <p className="text-sm text-slate-400">Loading interests…</p>}
        {catalogue.kind === "error" && (
          <div role="alert" className="flex items-center gap-3 text-sm text-amber-300">
            <span>{messageFor("NETWORK_ERROR")}</span>
            <button
              type="button"
              onClick={() => {
                setCatalogue({ kind: "loading" });
                loadInterests();
              }}
              className="rounded-md border border-amber-400/40 px-2 py-1 hover:bg-amber-400/10"
            >
              Retry
            </button>
          </div>
        )}
        {catalogue.kind === "ready" && (
          <div className="flex flex-wrap gap-2">
            {catalogue.interests.map((interest) => {
              const checked = selected.includes(interest.slug);
              return (
                <label
                  key={interest.slug}
                  className={`cursor-pointer rounded-full border px-3 py-1.5 text-sm transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-indigo-500 ${
                    checked
                      ? "border-indigo-400 bg-indigo-500/20 text-indigo-100"
                      : "border-slate-700 text-slate-300 hover:border-slate-500"
                  }`}
                >
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={checked}
                    onChange={() => toggleInterest(interest.slug)}
                  />
                  {interest.name}
                </label>
              );
            })}
          </div>
        )}
        {fieldErrors.interest_slugs && (
          <p role="alert" className="text-xs text-red-400">
            {fieldErrors.interest_slugs}
          </p>
        )}
      </fieldset>

      <fieldset
        className="flex flex-col gap-3 rounded-xl border border-slate-800 bg-slate-900/60 p-4"
        aria-describedby={fieldErrors.sponsor_consent ? "sponsor_consent-error" : undefined}
      >
        <legend className="px-1 text-sm font-medium text-slate-200">
          Sponsor data sharing
        </legend>
        <p className="text-xs text-slate-400">
          Separate from registering, and entirely your choice: may we share your registration
          details with the event&apos;s sponsors? You can register either way.
        </p>
        <label className="flex items-center gap-3 text-sm text-slate-200">
          <input
            type="radio"
            name="sponsor_consent"
            value="yes"
            checked={consent === "yes"}
            onChange={() => setConsent("yes")}
            className="h-4 w-4 accent-indigo-500"
          />
          Yes, share my details with sponsors
        </label>
        <label className="flex items-center gap-3 text-sm text-slate-200">
          <input
            type="radio"
            name="sponsor_consent"
            value="no"
            checked={consent === "no"}
            onChange={() => setConsent("no")}
            className="h-4 w-4 accent-indigo-500"
          />
          No, don&apos;t share my details
        </label>
        {fieldErrors.sponsor_consent && (
          <p id="sponsor_consent-error" role="alert" className="text-xs text-red-400">
            {fieldErrors.sponsor_consent}
          </p>
        )}
      </fieldset>

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
        {submitting ? "Registering…" : "Register and get my code"}
      </button>
    </form>
  );
}
