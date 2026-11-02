"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";
import {
  STATUS_TEXT,
  bookMentor,
  cancelMentor,
  fetchMentorSessions,
  mentorFailure,
  type MentorProgramme,
  type MentorSession,
} from "@/lib/mentors";

type State = { kind: "loading" } | { kind: "hidden" } | { kind: "ok"; data: MentorProgramme };
type Notice = { tone: "ok" | "warn"; text: string; place: number } | null;

// Meet the Mentor: 20-minute groups at the Connection Hub. The host scans you
// at the end; points are shown only once the server has recorded them.
export function MentorGroups() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const apply = useCallback((r: ApiResult<MentorProgramme>) => {
    // Signed-out and error states are explained by the page around it.
    setState(r.ok ? { kind: "ok", data: r.data } : { kind: "hidden" });
  }, []);
  useEffect(() => {
    let active = true;
    void fetchMentorSessions().then((r) => {
      if (active) apply(r);
    });
    return () => { active = false; };
  }, [apply]);

  async function run(place: number, action: () => Promise<ApiResult<unknown>>, success: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const r = await action();
      setNotice(r.ok ? { tone: "ok", text: success, place } : { tone: "warn", text: mentorFailure(r, messageFor), place });
      apply(await fetchMentorSessions());
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (state.kind !== "ok") return null;
  const { sessions, timezone } = state.data;
  // No mentor group in the programme (13 Nov agenda): the section stays out of the way.
  if (sessions.length === 0) return null;
  const holding = sessions.some((s) => s.booking && s.booking.status !== "attended");

  function card(s: MentorSession) {
    const own = s.booking;
    const full = s.seats_left === 0 && s.waitlist_left === 0;
    return (
      <li key={s.id} className="fm-card fm-section" data-testid={`mentor-${s.id}`}>
        <div className="fm-row-between">
          <h3 className="fm-h3">{s.title}</h3>
          <span className="fm-badge">{eventTime(s.starts_at, timezone, language)}–{eventTime(s.ends_at, timezone, language)}</span>
        </div>
        {s.mentor_name && <p className="fm-small">{s.mentor_name}</p>}
        <p className="fm-micro">
          {s.seats_left > 0
            ? t("{n} of {total} places left").replace("{n}", String(s.seats_left)).replace("{total}", String(s.capacity))
            : s.waitlist_left > 0 ? t("Group full · waiting list open") : t("Group and waiting list full")}
        </p>
        {own && (
          <p className="fm-strong">
            {t(STATUS_TEXT[own.status])}
            {own.status === "waitlisted" && own.position !== null && ` ${t("Place in line: {n}").replace("{n}", String(own.position))}`}
          </p>
        )}
        {!own && s.bookable && !full && (
          <button
            type="button"
            disabled={busy || holding}
            className="fm-btn fm-btn-primary fm-btn-block"
            onClick={() => void run(s.id, () => bookMentor(s.id, s.revision, crypto.randomUUID()), s.seats_left > 0 ? "Your place is confirmed." : "You are on the waiting list.")}
          >
            {t(s.seats_left > 0 ? "Join this group" : "Join the waiting list")}
          </button>
        )}
        {own && own.status !== "attended" && s.cancellable && (
          <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => void run(s.id, () => cancelMentor(own), "Your place was cancelled.")}>
            {t("Cancel my place")}
          </button>
        )}
        {notice?.place === s.id && (
          <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{t(notice.text)}</p>
        )}
      </li>
    );
  }

  return (
    <section className="fm-section" aria-labelledby="mentor-groups">
      <h2 id="mentor-groups" className="fm-eyebrow">{t("Meet the Mentor")}</h2>
      <p className="fm-small">{t("20 minutes with a speaker in a group of up to 6. At the end the host scans your QR: +5 points, once.")}</p>
      <ul className="fm-list">{sessions.map(card)}</ul>
      {holding && <p className="fm-micro">{t("You can hold one mentor group place at a time.")}</p>}
    </section>
  );
}
