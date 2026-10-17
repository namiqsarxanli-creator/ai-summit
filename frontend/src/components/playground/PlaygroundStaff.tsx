"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, type ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";
import {
  STATUS_LABEL,
  admitPlayground,
  failureText,
  slotLabel,
  type PlaygroundBooking,
  type PlaygroundProgram,
} from "@/lib/playground";

type Member = PlaygroundBooking & { name: string };
type Notice = { tone: "ok" | "warn"; text: string } | null;

// Booth staff: the people booked or queueing for one session of THEIR booth (the
// server refuses any other booth), and "Confirm arrival". Arrival is not points:
// the end-of-session scan belongs to the attendance flow.
export function PlaygroundStaff() {
  const { t, language } = useLanguage();
  const [program, setProgram] = useState<PlaygroundProgram | null>(null);
  const [slot, setSlot] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  // Only the newest list request may paint (switching slots quickly).
  const newest = useRef(0);

  useEffect(() => {
    let active = true;
    void apiGet<PlaygroundProgram>("/api/staff/playground").then((result) => {
      if (!active) return;
      if (result.ok) {
        setProgram(result.data);
        setSlot(String(result.data.slots[0]?.id ?? ""));
      } else setNotice({ tone: "warn", text: messageFor(result.error.code) });
    });
    return () => { active = false; };
  }, []);

  const applyList = useCallback((ticket: number, result: ApiResult<{ bookings: Member[] }>) => {
    if (ticket !== newest.current) return;
    if (result.ok) setMembers(result.data.bookings);
    else setNotice({ tone: "warn", text: messageFor(result.error.code) });
  }, []);
  const refresh = useCallback(async () => {
    if (!slot) return;
    const ticket = ++newest.current;
    applyList(ticket, await apiGet<{ bookings: Member[] }>(`/api/staff/playground/slots/${slot}`));
  }, [slot, applyList]);

  useEffect(() => {
    if (!slot) return;
    let active = true;
    const ticket = ++newest.current;
    void apiGet<{ bookings: Member[] }>(`/api/staff/playground/slots/${slot}`).then((r) => {
      if (active) applyList(ticket, r);
    });
    return () => { active = false; };
  }, [slot, applyList]);

  async function admit(member: Member) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const result = await admitPlayground(member);
      if (!result.ok) setNotice({ tone: "warn", text: failureText(result, messageFor) });
      else if (result.data.admitted) setNotice({ tone: "ok", text: "Arrival confirmed." });
      else setNotice({ tone: "warn", text: "More than 3 minutes late: the reserved place went to the queue. The attendee can join the queue again." });
      await refresh();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="fm-stack-lg">
      <p className="fm-small">{t("Confirm arrivals only at your booth. This does not award points.")}</p>
      {notice && (
        <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
          {t(notice.text)}
        </p>
      )}
      {program === null && !notice && <p role="status" className="fm-note">{t("Loading Playground…")}</p>}
      {program && program.slots.length === 0 && <p className="fm-empty">{t("The organiser has not opened Playground bookings yet.")}</p>}
      {program && program.slots.length > 0 && (
        <>
          <label className="fm-field">
            <span className="fm-label">{t("Playground slots")}</span>
            <select
              className="fm-input"
              value={slot}
              disabled={busy}
              onChange={(e) => { setMembers([]); setNotice(null); setSlot(e.target.value); }}
            >
              {program.slots.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.booth} · {slotLabel(s)} · {eventTime(s.starts_at, s.timezone, language)}
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={busy} className="fm-btn fm-btn-secondary" onClick={() => void refresh()}>
            {t("Refresh programme")}
          </button>
          {members.length === 0 && <p className="fm-empty">{t("Nobody has booked or queued for this session yet.")}</p>}
          <ul className="fm-list">
            {members.map((m) => (
              <li key={m.id} className="fm-card fm-section">
                <h2 className="fm-h3">{m.name}</h2>
                <p className="fm-small">{t(STATUS_LABEL[m.status] ?? m.status)}</p>
                {(m.status === "confirmed" || m.status === "waitlisted") && (
                  <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void admit(m)}>
                    {t("Confirm arrival")}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
