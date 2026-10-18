"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiPost, apiRequest, type ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Table = { id: number; event_id: number; label: string; is_active: boolean };
type MentorSessionRow = {
  id: number;
  event_id: number;
  code: string;
  title: string;
  mentor_name: string | null;
  starts_at: string;
  ends_at: string;
  capacity: number;
  waitlist_capacity: number;
  is_open: boolean;
};

const input = "min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-base text-(--fm-ink)";
const int = (v: string) => (v.trim() === "" ? null : Number(v.trim()));

function Field({ label, value, set }: { label: string; value: string; set: (v: string) => void }) {
  const { t } = useLanguage();
  return (
    <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">
      {t(label)}
      <input className={input} value={value} onChange={(e) => set(e.target.value)} />
    </label>
  );
}

const loadAll = () =>
  Promise.all([apiGet<Table[]>("/api/admin/match-tables"), apiGet<MentorSessionRow[]>("/api/admin/mentor-sessions")]);

// Connection Hub configuration: the Match Corner tables and the Meet the Mentor
// groups. Nothing is pre-filled; the organiser enters the real tables, mentors and
// times. Every change is audited by the server.
export function ConnectionHubAdmin() {
  const { t } = useLanguage();
  const [tables, setTables] = useState<Table[]>([]);
  const [sessions, setSessions] = useState<MentorSessionRow[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [eventId, setEventId] = useState("");
  const [label, setLabel] = useState("");
  const [tableId, setTableId] = useState("");
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [mentor, setMentor] = useState("");
  const [starts, setStarts] = useState("");
  const [capacity, setCapacity] = useState("6");
  const [waitlist, setWaitlist] = useState("3");
  const [sessionId, setSessionId] = useState("");

  const apply = useCallback((tb: ApiResult<Table[]>, ms: ApiResult<MentorSessionRow[]>) => {
    if (tb.ok) setTables(tb.data);
    if (ms.ok) setSessions(ms.data);
    const failed = !tb.ok ? tb : !ms.ok ? ms : null;
    if (failed && !failed.ok) setError(messageFor(failed.error.code));
  }, []);
  useEffect(() => {
    let active = true;
    void loadAll().then(([tb, ms]) => {
      if (active) apply(tb, ms);
    });
    return () => { active = false; };
  }, [apply]);

  async function change(run: () => Promise<ApiResult<unknown>>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setNotice(""); setError("");
    try {
      const r = await run();
      if (r.ok) {
        setNotice("Change saved and recorded in the admin audit.");
        const [tb, ms] = await loadAll();
        apply(tb, ms);
      } else setError(messageFor(r.error.code));
    } finally {
      inFlight.current = false; setBusy(false);
    }
  }

  return (
    <div className="grid gap-5">
      <section className="grid gap-2">
        <h3 className="font-semibold">{t("Match Corner tables")}</h3>
        {tables.length === 0 ? <p className="text-sm text-(--fm-muted)">{t("Nobody yet.")}</p> : (
          <ul className="grid gap-1 text-sm">
            {tables.map((x) => <li key={x.id}>#{x.id} {x.label}{x.is_active ? "" : ` · ${t("inactive")}`}</li>)}
          </ul>
        )}
      </section>
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Add a table")}</legend>
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Table label" value={label} set={setLabel} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiPost("/api/admin/match-tables", { event_id: int(eventId), label: label.trim() }))}>{t("Save table")}</button>
        <Field label="Table ID" value={tableId} set={setTableId} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/match-tables/${int(tableId)}`, "PATCH", { is_active: false }))}>{t("Take the table out of use")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/match-tables/${int(tableId)}`, "PATCH", { is_active: true }))}>{t("Use the table again")}</button>
      </fieldset>

      <section className="grid gap-2">
        <h3 className="font-semibold">{t("Meet the Mentor groups")}</h3>
        {sessions.length === 0 ? <p className="text-sm text-(--fm-muted)">{t("Nobody yet.")}</p> : (
          <ul className="grid gap-1 text-sm">
            {sessions.map((s) => (
              <li key={s.id}>#{s.id} {s.code} · {s.title}{s.mentor_name ? ` · ${s.mentor_name}` : ""} · {s.starts_at} · {s.capacity}+{s.waitlist_capacity}{s.is_open ? "" : ` · ${t("closed")}`}</li>
            ))}
          </ul>
        )}
      </section>
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Add a mentor group")}</legend>
        <p className="text-sm text-(--fm-ink-soft)">{t("The PDF times are 13:10, 13:35 and 15:35; each group lasts 20 minutes. Enter the real mentor as announced.")}</p>
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Code" value={code} set={setCode} />
        <Field label="Title" value={title} set={setTitle} />
        <Field label="Mentor (as announced; optional)" value={mentor} set={setMentor} />
        <Field label="Starts (with offset, e.g. 2026-11-13T13:10+04:00)" value={starts} set={setStarts} />
        <Field label="Group size (1-6)" value={capacity} set={setCapacity} />
        <Field label="Waiting list size" value={waitlist} set={setWaitlist} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiPost("/api/admin/mentor-sessions", { event_id: int(eventId), code: code.trim(), title: title.trim(), mentor_name: mentor.trim() || null, starts_at: starts.trim(), capacity: int(capacity), waitlist_capacity: int(waitlist) }))}>{t("Save mentor group")}</button>
      </fieldset>
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Change a mentor group")}</legend>
        <p className="text-sm text-(--fm-ink-soft)">{t("A group cannot move while people hold places, nor shrink below its seated people.")}</p>
        <Field label="Mentor group ID" value={sessionId} set={setSessionId} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/mentor-sessions/${int(sessionId)}`, "PATCH", { capacity: int(capacity), waitlist_capacity: int(waitlist) }))}>{t("Save the sizes")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/mentor-sessions/${int(sessionId)}`, "PATCH", { is_open: false }))}>{t("Close bookings")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/mentor-sessions/${int(sessionId)}`, "PATCH", { is_open: true }))}>{t("Open bookings")}</button>
      </fieldset>

      {notice && <p role="status" className="rounded-xl border border-(--fm-ok-line) bg-(--fm-ok-soft) p-3 text-sm text-(--fm-ok)">{t(notice)}</p>}
      {error && <p role="alert" className="text-(--fm-warn)">{t(error)}</p>}
    </div>
  );
}
