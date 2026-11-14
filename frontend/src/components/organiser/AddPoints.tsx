"use client";

import { useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { CATEGORY_LABEL, OUTCOME_TEXT, type AwardOutcome } from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { confirmAction } from "@/lib/confirm";

const SOURCES = ["PROFILE", "PANEL", "QMETER", "MASTERCLASS", "PLAYGROUND", "CONNECTION"] as const;

type Result = { name: string; outcome: AwardOutcome; points: number };
type Notice = { tone: "ok" | "warn"; text: string } | null;

// A scan was missed although the person took part: the Game Master adds the points,
// with a reason. The server keeps every category's maximum; Founder follows by itself.
export function AddPoints({ onAdded }: { onAdded: () => void }) {
  const { t } = useLanguage();
  const [attendee, setAttendee] = useState("");
  const [source, setSource] = useState<(typeof SOURCES)[number]>("MASTERCLASS");
  const [points, setPoints] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  // One key per intended award: a retry after a lost answer adds nothing.
  const command = useRef<string | null>(null);

  const id = Number(attendee);
  const amount = Number(points);
  const ready = Number.isInteger(id) && id > 0 && Number.isInteger(amount) && amount > 0 && reason.trim().length >= 3;

  async function submit() {
    if (!ready || busy) return;
    if (!(await confirmAction(t("Add {p} points to attendee {n}?").replace("{p}", String(amount)).replace("{n}", String(id))))) return;
    setBusy(true);
    setNotice(null);
    command.current ??= crypto.randomUUID();
    const r = await apiPost<Result>("/api/staff/points-log/award", {
      user_id: id, source, points: amount, reason: reason.trim(), command_id: command.current,
    });
    setBusy(false);
    if (!r.ok) {
      setNotice({ tone: "warn", text: messageFor(r.error.code) });
      return;
    }
    command.current = null;
    if (r.data.outcome === "awarded") {
      setNotice({ tone: "ok", text: `${r.data.name} · +${r.data.points}` });
      setAttendee(""); setPoints(""); setReason("");
      onAdded();
    } else {
      setNotice({ tone: "warn", text: `${r.data.name} · ${t(OUTCOME_TEXT[r.data.outcome])}` });
    }
  }

  return (
    <details className="fm-card fm-section">
      <summary className="fm-strong">{t("Add points")}</summary>
      <p className="fm-small">{t("For a scan that was missed although the person took part. Every category's maximum still applies; Beyond Now Founder follows by itself.")}</p>
      <label className="fm-field">
        <span className="fm-label">{t("Attendee ID")}</span>
        <input className="fm-input" inputMode="numeric" value={attendee} onChange={(e) => setAttendee(e.target.value.trim())} />
      </label>
      <label className="fm-field">
        <span className="fm-label">{t("Category")}</span>
        <select className="fm-input" value={source} onChange={(e) => setSource(e.target.value as (typeof SOURCES)[number])}>
          {SOURCES.map((s) => <option key={s} value={s}>{t(CATEGORY_LABEL[s] ?? s)}</option>)}
        </select>
      </label>
      <label className="fm-field">
        <span className="fm-label">{t("Points")}</span>
        <input className="fm-input" inputMode="numeric" value={points} onChange={(e) => setPoints(e.target.value.trim())} />
      </label>
      <label className="fm-field">
        <span className="fm-label">{t("Reason")}</span>
        <input className="fm-input" maxLength={280} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <button type="button" className="fm-btn fm-btn-primary fm-btn-block" disabled={!ready || busy} onClick={() => void submit()}>{t("Add points")}</button>
      {notice && <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{notice.text}</p>}
    </details>
  );
}
