"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { messageFor } from "@/lib/errors";

const input = "min-h-11 w-full rounded-xl border border-slate-600 bg-slate-900 p-3 text-base text-white";

// Opens a booth's 36 fifteen-minute Playground sessions (09:00-18:00 in the
// event's time zone). Re-running it adds only missing sessions; existing ones and
// their bookings are never changed. Audited like every admin change.
export function PlaygroundAdmin() {
  const { t } = useLanguage();
  const [event, setEvent] = useState("");
  const [booth, setBooth] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const inFlight = useRef(false);

  async function prepare() {
    if (inFlight.current) return;
    const eventId = Number(event);
    const boothId = Number(booth);
    if (![eventId, boothId].every((n) => Number.isSafeInteger(n) && n >= 1)) {
      setNotice(t("Enter a whole number greater than zero."));
      return;
    }
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await apiPost<{ created: number }>("/api/admin/playground/prepare", { event_id: eventId, booth_id: boothId });
      setNotice(result.ok ? `${t("Slots created")}: ${result.data.created}` : t(messageFor(result.error.code)));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="grid min-w-0 max-w-lg gap-3">
      <p className="text-sm text-slate-300">{t("Create 36 fifteen-minute slots for a participating booth. Existing bookings are preserved.")}</p>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        {t("Event ID")}
        <input className={input} inputMode="numeric" value={event} onChange={(e) => setEvent(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        {t("Booth ID")}
        <input className={input} inputMode="numeric" value={booth} onChange={(e) => setBooth(e.target.value)} />
      </label>
      <button type="button" className="rounded bg-indigo-600 p-2" disabled={busy} onClick={() => void prepare()}>
        {t("Open Playground slots")}
      </button>
      {notice && <p role="status" className="text-sm text-slate-100">{notice}</p>}
      <Link className="text-sm text-cyan-300 underline" href="/staff/playground">{t("Playground arrivals and queue")} →</Link>
    </div>
  );
}
