"use client";

import { useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import { setSponsorConsent, type AttendeeProfile } from "@/lib/profile";

type Consent = Pick<
  AttendeeProfile,
  "sponsor_consent" | "sponsor_consent_at" | "sponsor_consent_version"
>;

// Sponsor data sharing is its OWN choice, saved by its own button the moment it
// is pressed: it is not a field of the profile form, so editing a name never
// changes it and changing it never needs a form to be saved. The server writes
// the new value and its audit record in one transaction; pressing the button for
// a value that is already in force writes nothing.
export function ConsentControl({
  consent,
  onChanged,
  onSignedOut,
}: {
  consent: Consent;
  onChanged: (next: Consent) => void;
  onSignedOut: () => void;
}) {
  const { t, language } = useLanguage();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function choose(granted: boolean) {
    if (saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    const result = await setSponsorConsent(granted);
    setSaving(false);
    if (result.ok) {
      onChanged({
        sponsor_consent: result.data.sponsor_consent,
        sponsor_consent_at: result.data.sponsor_consent_at,
        sponsor_consent_version: result.data.consent_version,
      });
      setSaved(true);
      return;
    }
    if (result.error.code === "UNAUTHENTICATED") return onSignedOut();
    // The value on screen is still the one that is in force: nothing was saved.
    setError(messageFor(result.error.code));
  }

  const since = consent.sponsor_consent && consent.sponsor_consent_at
    ? new Date(consent.sponsor_consent_at).toLocaleDateString(language, {
        year: "numeric",
        month: "long",
        day: "numeric",
      })
    : null;

  return (
    <section
      className="fm-card fm-card-tint fm-section"
      aria-busy={saving}
    >
      <h2 className="fm-eyebrow">{t("Sponsor data sharing")}</h2>
      <p className="fm-text">
        {consent.sponsor_consent
          ? t("You agreed to share your details with event sponsors.")
          : t("You have not agreed to share your details with event sponsors.")}
      </p>
      {since && (
        <p className="fm-small">
          {t("Agreed on")} {since}
        </p>
      )}
      <p className="fm-small">{t("Private — visible only to you.")}</p>
      <button
        type="button"
        onClick={() => choose(!consent.sponsor_consent)}
        disabled={saving}
        className="fm-btn fm-btn-secondary fm-btn-block"
      >
        {saving
          ? t("Saving your choice…")
          : consent.sponsor_consent
            ? t("Stop sharing my details")
            : t("Share my details with sponsors")}
      </button>
      {error && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(error)}
        </p>
      )}
      {saved && !error && (
        <p role="status" className="fm-status fm-status-ok">
          {t("Your choice was saved.")}
        </p>
      )}
    </section>
  );
}
