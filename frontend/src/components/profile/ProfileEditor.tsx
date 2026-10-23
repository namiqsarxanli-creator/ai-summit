"use client";

import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";

import { InterestCardFields } from "@/components/profile/InterestCardFields";
import { TextField } from "@/components/ui/TextField";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { fieldErrorsFrom, messageFor } from "@/lib/errors";
import {
  diffDrafts,
  draftOf,
  parseProfile,
  sameDraft,
  updateProfile,
  validateProfile,
  type AttendeeProfile,
  type ProfileDraft,
} from "@/lib/profile";

const FIELD_LABELS: Record<keyof ProfileDraft, string> = {
  first_name: "First name",
  last_name: "Last name",
  phone: "Phone",
  profession: "Profession / role",
  linkedin_url: "LinkedIn (optional)",
  interest_slugs: "Interests",
  company: "Company",
  interest_card: "Interest card",
};

// Edits the attendee's own details and interests.
//
// What the person typed is never thrown away by an error: a failed save (the
// network, a validation problem, an interest that was retired, a newer version
// saved elsewhere) leaves the form exactly as it was and says what happened. The
// only ways to lose it are the two explicit ones - Cancel (confirmed when there
// is anything to lose) and "Use the saved version" on a conflict.
export function ProfileEditor({
  profile,
  onSaved,
  onCancel,
  onBaseline,
  onSignedOut,
}: {
  profile: AttendeeProfile;
  onSaved: (profile: AttendeeProfile) => void;
  onCancel: () => void;
  // The server's current profile, when it moved on while this form was open.
  onBaseline: (profile: AttendeeProfile) => void;
  onSignedOut: () => void;
}) {
  const { t } = useLanguage();
  const [baseline, setBaseline] = useState(profile);
  const [draft, setDraft] = useState<ProfileDraft>(() => draftOf(profile));
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<AttendeeProfile | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const inFlight = useRef(false);

  const dirty = !sameDraft(draft, draftOf(baseline));

  // A reload or a closed tab would lose the draft: ask first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // The booth-recommendation interests are not edited here any more (the
  // interest-card topics drive recommendations); the saved ones are sent back
  // unchanged. Names are still needed to describe a conflicting newer version.
  const names = new Map<string, string>();
  for (const i of baseline.interests) names.set(i.slug, i.name);
  for (const i of conflict?.interests ?? []) names.set(i.slug, i.name);
  const nameOf = (slug: string) => names.get(slug) ?? slug;

  function update(field: "first_name" | "last_name" | "phone" | "profession" | "linkedin_url" | "company") {
    return (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      setDraft((current) => ({ ...current, [field]: value }));
      setFieldErrors((current) => ({ ...current, [field]: "" }));
    };
  }

  async function save() {
    if (inFlight.current) return;
    setFormError(null);
    const clientErrors = validateProfile(draft, baseline.interest_card);
    setFieldErrors(clientErrors);
    if (Object.keys(clientErrors).length > 0) return;

    inFlight.current = true;
    setSaving(true);
    const result = await updateProfile(draft, baseline.version);
    inFlight.current = false;
    setSaving(false);

    if (result.ok) {
      onSaved(result.data);
      return;
    }
    const { code, details } = result.error;
    if (code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED") return onSignedOut();
    if (code === "PROFILE_CONFLICT") {
      const current = parseProfile(details["profile"]);
      if (current) {
        if (sameDraft(draft, draftOf(current))) return onSaved(current); // nothing left to decide
        setBaseline(current);
        onBaseline(current);
        setConflict(current);
        return;
      }
    }
    if (code === "UNKNOWN_INTEREST") {
      setFormError("Some of your interests are no longer available. Remove them and save again.");
      return;
    }
    if (code === "VALIDATION_ERROR") setFieldErrors(fieldErrorsFrom(details));
    setFormError(messageFor(code));
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void save();
  }

  function requestCancel() {
    if (dirty) setConfirmingCancel(true);
    else onCancel();
  }

  const diffs = conflict
    ? diffDrafts(draft, draftOf(conflict), nameOf, t)
    : [];

  return (
    <form
      noValidate
      onSubmit={onSubmit}
      aria-label={t("Edit profile")}
      aria-busy={saving}
      className="fm-form"
    >
      {conflict && (
        <section role="alert" className="fm-alert fm-alert-warn fm-stack">
          <h2>
            {t("Your profile was changed on another device")}
          </h2>
          <p>
            {t("It was saved after you opened it here — probably by you, on another device. Nothing you typed was lost: your changes are still in the form.")}
          </p>
          <dl className="fm-dl">
            {diffs.map((diff) => (
              <div key={diff.field}>
                <dt>{t(FIELD_LABELS[diff.field])}</dt>
                <dd>
                  <span className="fm-micro">{t("Your version")}: </span>
                  {diff.mine || t("Not set")}
                </dd>
                <dd>
                  <span className="fm-micro">{t("Saved version")}: </span>
                  {diff.theirs || t("Not set")}
                </dd>
              </div>
            ))}
          </dl>
          <div className="fm-row">
            <button
              type="submit"
              disabled={saving}
              className="fm-btn fm-btn-primary"
            >
              {saving ? t("Saving…") : t("Save my changes anyway")}
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => {
                setDraft(draftOf(conflict));
                setConflict(null);
                setFieldErrors({});
              }}
              className="fm-btn fm-btn-secondary"
            >
              {t("Use the saved version")}
            </button>
          </div>
        </section>
      )}

      <section className="fm-grid-2">
        <TextField
          id="edit_first_name"
          label="First name"
          autoComplete="given-name"
          value={draft.first_name}
          onChange={update("first_name")}
          error={fieldErrors.first_name || undefined}
          required
        />
        <TextField
          id="edit_last_name"
          label="Last name"
          autoComplete="family-name"
          value={draft.last_name}
          onChange={update("last_name")}
          error={fieldErrors.last_name || undefined}
          required
        />
        <TextField
          id="edit_email"
          label="Email"
          type="email"
          value={baseline.email}
          readOnly
          aria-readonly="true"
          hint="Your email can't be changed here."
        />
        <TextField
          id="edit_phone"
          label="Phone"
          type="tel"
          autoComplete="tel"
          value={draft.phone}
          onChange={update("phone")}
          error={fieldErrors.phone || undefined}
          hint="Never shown on your public networking card."
          required
        />
        <TextField
          id="edit_profession"
          label="Profession / role"
          autoComplete="organization-title"
          value={draft.profession}
          onChange={update("profession")}
          error={fieldErrors.profession || undefined}
          required
        />
        <TextField
          id="edit_company"
          label="Company"
          autoComplete="organization"
          maxLength={200}
          hint="No company? Write Student or Self-employed."
          required
          value={draft.company}
          onChange={update("company")}
          error={fieldErrors.company || undefined}
        />
        <TextField
          id="edit_linkedin_url"
          label="LinkedIn (optional)"
          type="url"
          inputMode="url"
          placeholder="https://www.linkedin.com/in/your-name"
          value={draft.linkedin_url}
          onChange={update("linkedin_url")}
          error={fieldErrors.linkedin_url || undefined}
        />
      </section>

      <InterestCardFields
        idPrefix="edit"
        card={draft.interest_card}
        onChange={(next) => {
          setDraft((current) => ({ ...current, interest_card: next }));
          setFieldErrors((current) => ({ ...current, interest_card: "" }));
        }}
        error={fieldErrors.interest_card || undefined}
      />

      {formError && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(formError)}
        </p>
      )}

      {confirmingCancel && (
        <div role="alertdialog" aria-label={t("Discard your changes?")} className="fm-card fm-card-tint fm-stack">
          <p className="fm-strong">{t("Discard your changes?")}</p>
          <div className="fm-row">
            <button
              type="button"
              onClick={onCancel}
              className="fm-btn fm-btn-primary"
            >
              {t("Discard changes")}
            </button>
            <button
              type="button"
              onClick={() => setConfirmingCancel(false)}
              className="fm-btn fm-btn-secondary"
            >
              {t("Keep editing")}
            </button>
          </div>
        </div>
      )}

      <div className="fm-stack" style={{ gap: 12 }}>
        <p role="status" data-dirty={dirty ? "true" : "false"} className="fm-dirty">
          <span aria-hidden="true" className="fm-dirty-dot" />
          {dirty ? t("Unsaved changes") : t("No changes yet")}
        </p>
        <div className="fm-row">
          <button
            type="submit"
            disabled={saving || !dirty}
            className="fm-btn fm-btn-primary fm-grow"
          >
            {saving && !conflict ? t("Saving…") : t("Save changes")}
          </button>
          <button
            type="button"
            onClick={requestCancel}
            disabled={saving}
            className="fm-btn fm-btn-secondary fm-grow"
          >
            {t("Cancel")}
          </button>
        </div>
      </div>
    </form>
  );
}
