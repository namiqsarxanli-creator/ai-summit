"use client";

import { useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, type ApiResult } from "@/lib/api";
import {
  OUTCOME_TEXT,
  awardQmeter,
  cardTokenFrom,
  completePlayground,
  doorScan,
  fetchMyScopes,
  fetchPanels,
  identifyAttendee,
  scanFailure,
  type Identified,
  type PublicPanel,
  type Scope,
} from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { eventTime, type Program } from "@/lib/masterclasses";
import { completeMentor, fetchHostSessions, mentorFailure, type HostSession } from "@/lib/mentors";
import { durableQueueSupported, enqueue, unreachable, type Owner, type ScanKind } from "@/lib/offlineQueue";
import { slotLabel, type PlaygroundProgram } from "@/lib/playground";
import { fetchStaffMe, type StaffMe } from "@/lib/staff";

import { OfflineQueuePanel } from "./OfflineQueuePanel";
import { QrCapture } from "./QrCapture";

type Notice = { tone: "ok" | "warn"; text: string } | null;
type Ready = {
  me: StaffMe;
  scopes: Scope[];
  programme: Program | null;
  slots: PlaygroundProgram | null;
  panels: PublicPanel[];
  mentor: { timezone: string; sessions: HostSession[] } | null;
};
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: Ready };

// One desk for every scanning job the server lets THIS account do: a masterclass
// door (MASTERCLASS_DOOR scope), the end of this booth's Playground sessions
// (booth staff), Qmeter questions (GAME_MASTER), the end of a mentor group
// (MENTOR_HOST). The list of jobs comes from the
// server; each scan is re-checked there (a revoked scope fails at once).
// `jobs` lets the organiser panel show the scanning jobs and Qmeter in separate
// sections; it only hides desks, it never adds one the scopes do not allow.
export type DeskJobs = "all" | "scans" | "qmeter";

export function AttendanceDesk({ jobs = "all" }: { jobs?: DeskJobs }) {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    void (async () => {
      const me = await fetchStaffMe();
      if (!me.ok) {
        if (active) setState({ kind: "error", text: messageFor(me.error.code) });
        return;
      }
      const [scopes, programme, slots, panels, mentor] = await Promise.all([
        fetchMyScopes(),
        apiGet<Program>("/api/masterclasses"),
        me.data.role === "BOOTH_STAFF" ? apiGet<PlaygroundProgram>("/api/staff/playground") : Promise.resolve(null),
        fetchPanels(),
        fetchHostSessions(),
      ]);
      if (!active) return;
      setState({
        kind: "ok",
        data: {
          me: me.data,
          scopes: scopes.ok ? scopes.data.scopes : [],
          programme: programme.ok ? programme.data : null,
          slots: slots && slots.ok ? slots.data : null,
          panels: panels.ok ? panels.data : [],
          mentor: mentor.ok ? mentor.data : null,
        },
      });
    })();
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const { data } = state;
  const scans = jobs !== "qmeter";
  const doors = scans ? data.scopes.filter((s) => s.scope === "MASTERCLASS_DOOR" && s.target_id !== null) : [];
  const gameMaster = jobs !== "scans" && data.scopes.some((s) => s.scope === "GAME_MASTER");
  const booth = scans && data.me.role === "BOOTH_STAFF" && data.slots !== null;
  const hosts = scans && data.scopes.some((s) => s.scope === "MENTOR_HOST") && (data.mentor?.sessions.length ?? 0) > 0;
  if (!doors.length && !gameMaster && !booth && !hosts) {
    return <p className="fm-alert fm-alert-info">{t(jobs === "qmeter"
      ? "Qmeter needs the Game Master scope. Ask an administrator."
      : "No scanning job is assigned to this account. Ask an administrator.")}</p>;
  }
  // Queued scans are filed under this account and this dataset (lib/offlineQueue.ts).
  const owner: Owner = { queue_owner: data.me.queue_owner, dataset_generation: data.me.dataset_generation, name: data.me.full_name };
  return (
    <div className="fm-stack-lg">
      {scans && <OfflineQueuePanel owner={owner} />}
      {doors.length > 0 && <DoorDesk doors={doors.map((d) => d.target_id as number)} programme={data.programme} owner={owner} />}
      {booth && data.slots && <PlaygroundDesk slots={data.slots} owner={owner} />}
      {gameMaster && <QmeterDesk panels={data.panels} />}
      {hosts && data.mentor && <MentorDesk sessions={data.mentor.sessions} timezone={data.mentor.timezone} owner={owner} />}
    </div>
  );
}

// The network did not answer: keep the scan on this phone with its own replay
// id and the time it was made; the queue panel sends it when the server is back.
async function queued(owner: Owner, kind: ScanKind, target: number, phase: "ENTRY" | "EXIT" | null, token: string): Promise<Notice> {
  const entry = await enqueue({ kind, target_id: target, phase, card_token: token }, owner);
  if (typeof window !== "undefined") window.dispatchEvent(new Event("facemark:offline-queue"));
  if (entry) return { tone: "warn", text: "No connection. The scan is saved on this phone and will be sent automatically. No points are shown until the server confirms." };
  return durableQueueSupported()
    ? { tone: "warn", text: "No connection, and this phone cannot store the scan. Try again when the connection is back." }
    : { tone: "warn", text: "No connection, and this browser cannot keep scans safely offline: the scan was NOT saved. Scan again when the connection is back." };
}

function NoticeLine({ notice }: { notice: Notice }) {
  const { t } = useLanguage();
  if (!notice) return null;
  return (
    <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
      {t(notice.text)}
    </p>
  );
}

function useOneAtATime() {
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  async function once<T>(run: () => Promise<T>): Promise<T | undefined> {
    if (inFlight.current) return undefined;
    inFlight.current = true;
    setBusy(true);
    try {
      return await run();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return { busy, once };
}

function DoorDesk({ doors, programme, owner }: { doors: number[]; programme: Program | null; owner: Owner }) {
  const { t, language } = useLanguage();
  const [chosen, setChosen] = useState(String(doors[0]));
  const [phase, setPhase] = useState<"ENTRY" | "EXIT">("ENTRY");
  const [notice, setNotice] = useState<Notice>(null);
  const { busy, once } = useOneAtATime();
  const titleOf = (id: number) => {
    for (const event of programme?.events ?? []) for (const block of event.blocks) {
      const mc = block.masterclasses.find((m) => m.id === id);
      if (mc) return `${mc.title} · ${eventTime(block.starts_at, event.timezone, language)}`;
    }
    return `#${id}`;
  };
  async function scan(text: string) {
    const token = cardTokenFrom(text);
    if (!token) { setNotice({ tone: "warn", text: "This is not an attendee QR code." }); return; }
    await once(async () => {
      const r = await doorScan(Number(chosen), token, phase);
      if (unreachable(r)) { setNotice(await queued(owner, "MASTERCLASS_DOOR", Number(chosen), phase, token)); return; }
      if (!r.ok) { setNotice({ tone: "warn", text: scanFailure(r, messageFor) }); return; }
      if (r.data.phase === "entry") {
        setNotice({ tone: "ok", text: `${r.data.name} · ${t(r.data.already ? "Already entered." : r.data.standing ? "Entry recorded (standing)." : "Entry recorded.")}` });
      } else {
        setNotice({ tone: "ok", text: `${r.data.name} · ${t(OUTCOME_TEXT[r.data.award?.outcome ?? "not_scored"])}` });
      }
    });
  }
  return (
    <section className="fm-card fm-section" aria-labelledby="desk-door">
      <h2 id="desk-door" className="fm-h3">{t("Masterclass door")}</h2>
      <label className="fm-field">
        <span className="fm-label">{t("Masterclass")}</span>
        <select className="fm-input" value={chosen} onChange={(e) => { setChosen(e.target.value); setNotice(null); }}>
          {doors.map((id) => <option key={id} value={id}>{titleOf(id)}</option>)}
        </select>
      </label>
      <fieldset className="fm-fieldset">
        <legend className="fm-legend">{t("Scan")}</legend>
        <div className="fm-choices">
          {(["ENTRY", "EXIT"] as const).map((p) => (
            <label key={p} className="fm-choice">
              <input type="radio" name="door-phase" checked={phase === p} onChange={() => setPhase(p)} />
              {t(p === "ENTRY" ? "Entry (first 10 minutes)" : "Exit (last 10 minutes)")}
            </label>
          ))}
        </div>
      </fieldset>
      <QrCapture paused={busy} onText={(text) => void scan(text)} manualLabel="Attendee code" />
      <NoticeLine notice={notice} />
    </section>
  );
}

function PlaygroundDesk({ slots, owner }: { slots: PlaygroundProgram; owner: Owner }) {
  const { t, language } = useLanguage();
  const [chosen, setChosen] = useState(String(slots.slots[0]?.id ?? ""));
  const [notice, setNotice] = useState<Notice>(null);
  const { busy, once } = useOneAtATime();
  async function scan(text: string) {
    const token = cardTokenFrom(text);
    if (!token) { setNotice({ tone: "warn", text: "This is not an attendee QR code." }); return; }
    await once(async () => {
      const r = await completePlayground(Number(chosen), token);
      if (unreachable(r)) { setNotice(await queued(owner, "PLAYGROUND_COMPLETE", Number(chosen), null, token)); return; }
      setNotice(r.ok
        ? { tone: "ok", text: `${r.data.name} · ${t(OUTCOME_TEXT[r.data.award.outcome])}` }
        : { tone: "warn", text: scanFailure(r, messageFor) });
    });
  }
  if (!slots.slots.length) return <p className="fm-empty">{t("The organiser has not opened Playground bookings yet.")}</p>;
  return (
    <section className="fm-card fm-section" aria-labelledby="desk-playground">
      <h2 id="desk-playground" className="fm-h3">{t("End of a Playground session")}</h2>
      <p className="fm-small">{t("Scan each member in the last 3 minutes of the session (until 5 minutes after it ends).")}</p>
      <label className="fm-field">
        <span className="fm-label">{t("Playground slots")}</span>
        <select className="fm-input" value={chosen} onChange={(e) => { setChosen(e.target.value); setNotice(null); }}>
          {slots.slots.map((s) => <option key={s.id} value={s.id}>{slotLabel(s)} · {eventTime(s.starts_at, s.timezone, language)}</option>)}
        </select>
      </label>
      <QrCapture paused={busy} onText={(text) => void scan(text)} manualLabel="Attendee code" />
      <NoticeLine notice={notice} />
    </section>
  );
}

function MentorDesk({ sessions, timezone, owner }: { sessions: HostSession[]; timezone: string; owner: Owner }) {
  const { t, language } = useLanguage();
  const [chosen, setChosen] = useState(String(sessions[0]?.id ?? ""));
  const [notice, setNotice] = useState<Notice>(null);
  const { busy, once } = useOneAtATime();
  async function scan(text: string) {
    const token = cardTokenFrom(text);
    if (!token) { setNotice({ tone: "warn", text: "This is not an attendee QR code." }); return; }
    await once(async () => {
      const r = await completeMentor(Number(chosen), token);
      if (unreachable(r)) { setNotice(await queued(owner, "MENTOR_COMPLETE", Number(chosen), null, token)); return; }
      setNotice(r.ok
        ? { tone: "ok", text: `${r.data.name} · ${t(OUTCOME_TEXT[r.data.award.outcome])}` }
        : { tone: "warn", text: mentorFailure(r, messageFor) });
    });
  }
  return (
    <section className="fm-card fm-section" aria-labelledby="desk-mentor">
      <h2 id="desk-mentor" className="fm-h3">{t("End of a mentor group")}</h2>
      <p className="fm-small">{t("Scan each participant from 5 minutes before the end until 10 minutes after it.")}</p>
      <label className="fm-field">
        <span className="fm-label">{t("Mentor group")}</span>
        <select className="fm-input" value={chosen} onChange={(e) => { setChosen(e.target.value); setNotice(null); }}>
          {sessions.map((s) => <option key={s.id} value={s.id}>{s.title} · {eventTime(s.starts_at, timezone, language)}</option>)}
        </select>
      </label>
      <QrCapture paused={busy} onText={(text) => void scan(text)} manualLabel="Attendee code" />
      <NoticeLine notice={notice} />
    </section>
  );
}

function QmeterDesk({ panels }: { panels: PublicPanel[] }) {
  const { t, language } = useLanguage();
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  // The person found, with the exact name and phone that were searched for.
  const [found, setFound] = useState<(Identified & { query: string }) | null>(null);
  // Every lookup takes a number; changing the name or phone takes a new one, so
  // an answer that arrives late - for what was typed BEFORE - is dropped.
  const lookup = useRef(0);
  const [panel, setPanel] = useState(String(panels[0]?.id ?? ""));
  const [reason, setReason] = useState("");
  // The Game Master confirms, explicitly, that the person found is the one who
  // asked; changing the name or phone forgets the person and the confirmation.
  const [confirmed, setConfirmed] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const { busy, once } = useOneAtATime();
  const fail = (r: Extract<ApiResult<unknown>, { ok: false }>) => setNotice({ tone: "warn", text: scanFailure(r, messageFor) });
  const query = `${name.trim()}\n${phone.trim()}`;
  const forget = () => { lookup.current += 1; setFound(null); setConfirmed(false); };
  // Only a person found for what the fields say NOW can be confirmed or paid.
  const current = found !== null && found.query === query ? found : null;
  return (
    <section className="fm-card fm-section" aria-labelledby="desk-qmeter">
      <h2 id="desk-qmeter" className="fm-h3">{t("Qmeter questions")}</h2>
      <p className="fm-small">{t("Check the question in Qmeter first. Identify the person by full name AND phone number.")}</p>
      <form className="fm-stack" onSubmit={(e) => {
        e.preventDefault();
        void once(async () => {
          forget();
          const mine = lookup.current;
          const asked = query;
          const r = await identifyAttendee(name, phone);
          if (mine !== lookup.current) return; // the fields changed meanwhile
          if (r.ok) { setFound({ ...r.data, query: asked }); setNotice(null); } else fail(r);
        });
      }}>
        <label className="fm-field"><span className="fm-label">{t("Full name")}</span><input className="fm-input" value={name} onChange={(e) => { setName(e.target.value); forget(); }} autoComplete="off" /></label>
        <label className="fm-field"><span className="fm-label">{t("Phone")}</span><input className="fm-input" inputMode="tel" value={phone} onChange={(e) => { setPhone(e.target.value); forget(); }} autoComplete="off" /></label>
        <button type="submit" disabled={busy || name.trim().length < 2 || phone.trim().length < 7} className="fm-btn fm-btn-secondary fm-btn-block">{t("Find the attendee")}</button>
      </form>
      {current && (
        <form className="fm-stack" onSubmit={(e) => {
          e.preventDefault();
          if (!confirmed || current.query !== query) return;
          const person = current;
          void once(async () => {
            const r = await awardQmeter(person.user_id, Number(panel), reason.trim());
            if (r.ok) setNotice({ tone: r.data.points > 0 ? "ok" : "warn", text: `${person.name} · ${t(OUTCOME_TEXT[r.data.outcome])}` });
            else fail(r);
          });
        }}>
          <p className="fm-alert fm-alert-info">{current.name} · {current.phone}</p>
          <label className="fm-field">
            <span className="fm-label">{t("Panel")}</span>
            <select className="fm-input" value={panel} onChange={(e) => setPanel(e.target.value)}>
              {panels.map((p) => <option key={p.id} value={p.id}>{p.title} · {eventTime(p.starts_at, p.timezone, language)}</option>)}
            </select>
          </label>
          <label className="fm-field"><span className="fm-label">{t("What you checked in Qmeter")}</span><input className="fm-input" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} /></label>
          <label className="fm-remember">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>{t("I confirm this is the person who asked the question in Qmeter.")}</span>
          </label>
          <button type="submit" disabled={busy || !confirmed || reason.trim().length < 3 || !panel} className="fm-btn fm-btn-primary fm-btn-block">{t("Record the question")}</button>
        </form>
      )}
      <NoticeLine notice={notice} />
    </section>
  );
}
