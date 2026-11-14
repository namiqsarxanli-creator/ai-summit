"use client";

import { useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiRequest } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Choice = { channel: "email"; available: boolean; enabled: boolean };

// The attendee's own opt-in to reminder emails (the evening before and 10 minutes
// before each confirmed item). Shown only while the organisers have the channel
// switched on; nothing is sent without this choice.
export function ReminderChoice() {
  const { t } = useLanguage();
  const [choice, setChoice] = useState<Choice | null>(null);
  const [error, setError] = useState("");
  const inFlight = useRef(false);

  useEffect(() => {
    let active = true;
    void apiGet<Choice>("/api/attendees/me/reminders").then((r) => {
      if (active && r.ok) setChoice(r.data);
    });
    return () => { active = false; };
  }, []);

  async function change(enabled: boolean) {
    if (inFlight.current) return;
    inFlight.current = true;
    setError("");
    try {
      const r = await apiRequest<Choice>("/api/attendees/me/reminders", "PUT", { enabled });
      if (r.ok) setChoice(r.data);
      else setError(messageFor(r.error.code));
    } finally {
      inFlight.current = false;
    }
  }

  if (!choice?.available) return null;
  return (
    <section className="fm-card fm-section" aria-labelledby="reminders">
      <h2 id="reminders" className="fm-eyebrow">{t("Reminders")}</h2>
      <label className="fm-choice">
        <input type="checkbox" checked={choice.enabled} onChange={(e) => void change(e.target.checked)} />
        {t("Email me the evening before and 10 minutes before each confirmed item.")}
      </label>
      <p className="fm-micro">{t("You can turn this off at any time; pending reminders are then cancelled.")}</p>
      {error && <p role="alert" className="fm-alert fm-alert-warn">{t(error)}</p>}
    </section>
  );
}
