"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { QrCapture } from "@/components/competition/QrCapture";
import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import { safeExternalUrl } from "@/lib/card";
import { resolveCardText } from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { labelOf } from "@/lib/interestCard";
import { eventTime } from "@/lib/masterclasses";
import {
  REASON_TEXT,
  blockPerson,
  cancelRequest,
  fetchConnections,
  fetchMeetings,
  fetchPeople,
  peopleFailure,
  promptText,
  respond,
  scanMessage,
  scanPerson,
  sendRequest,
  type ConnectionRow,
  type MeetingRequest,
  type Person,
  type ScanResult,
} from "@/lib/people";

type Data = { people: Person[]; requests: MeetingRequest[]; connections: ConnectionRow[]; timezone: string | null };
type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "error"; text: string } | { kind: "ok"; data: Data };
type Notice = { tone: "ok" | "warn"; text: string; place: string } | null;

// People to meet, meetings and connections. Suggestions carry no contact data;
// the contact opens only after both of you scan each other's QR.
export function PeopleHub() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const apply = useCallback((p: ApiResult<{ people: Person[] }>, m: ApiResult<{ timezone: string | null; requests: MeetingRequest[] }>, c: ApiResult<{ connections: ConnectionRow[] }>) => {
    if (!p.ok) {
      setState(p.error.code === "UNAUTHENTICATED" || p.error.code === "REGISTRATION_NOT_VERIFIED" ? { kind: "signed-out" } : { kind: "error", text: messageFor(p.error.code) });
      return;
    }
    setState({ kind: "ok", data: { people: p.data.people, requests: m.ok ? m.data.requests : [], connections: c.ok ? c.data.connections : [], timezone: m.ok ? m.data.timezone : null } });
  }, []);
  const load = useCallback(
    () => Promise.all([fetchPeople(), fetchMeetings(), fetchConnections()]).then(([p, m, c]) => apply(p, m, c)),
    [apply],
  );
  useEffect(() => {
    let active = true;
    void Promise.all([fetchPeople(), fetchMeetings(), fetchConnections()]).then(([p, m, c]) => {
      if (active) apply(p, m, c);
    });
    return () => { active = false; };
  }, [apply]);

  async function run(place: string, action: () => Promise<ApiResult<unknown>>, success: string | ((data: unknown) => string)) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const r = await action();
      setNotice(r.ok
        ? { tone: "ok", text: typeof success === "string" ? success : success(r.data), place }
        : { tone: "warn", text: peopleFailure(r, messageFor), place });
      await load();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const line = (place: string) =>
    notice?.place === place ? (
      <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{t(notice.text)}</p>
    ) : null;

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading people…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <section className="fm-stack">
        <p className="fm-alert fm-alert-info">{t("Sign in to meet people.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
      </section>
    );
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;

  const { people, requests, connections, timezone } = state.data;
  const zone = timezone ?? "UTC";
  const incoming = requests.filter((r) => r.direction === "incoming" && r.status === "pending");
  const outgoing = requests.filter((r) => r.direction === "outgoing" && r.status === "pending");
  const meetings = requests.filter((r) => r.status === "accepted" && r.meeting);
  // No Match Corner table (13 Nov): an accepted request has no table or time.
  const agreed = requests.filter((r) => r.status === "accepted" && !r.meeting);

  return (
    <div className="fm-stack-lg">
      <section className="fm-section" aria-labelledby="ppl-suggest">
        <h2 id="ppl-suggest" className="fm-eyebrow">{t("People to meet")}</h2>
        {people.length === 0 && <p className="fm-empty">{t("No strong match yet. Answer all four questions of your interest card to find people.")}</p>}
        <ul className="fm-list">
          {people.map((p) => (
            <li key={p.user_id} className="fm-card fm-section" data-testid={`person-${p.user_id}`}>
              <div className="fm-row-between">
                <h3 className="fm-h3">{p.name}</h3>
                <span className="fm-badge">{p.score}%</span>
              </div>
              <p className="fm-small">{p.profession}</p>
              <ul className="fm-stack" style={{ gap: 4 }}>
                {p.reasons.map((r) => <li key={r} className="fm-small">{t(REASON_TEXT[r])}</li>)}
              </ul>
              {p.shared_topics.length > 0 && (
                <ul className="fm-chips">{p.shared_topics.map((topic) => <li key={topic} className="fm-chip">{t(labelOf("topics", topic))}</li>)}</ul>
              )}
              <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void run(`p${p.user_id}`, () => sendRequest(p.user_id, crypto.randomUUID()), "Request sent.")}>{t("Let's meet")}</button>
              <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => { if (window.confirm(t("Block this person? You will not see each other here."))) void run(`p${p.user_id}`, () => blockPerson(p.user_id), "Blocked."); }}>{t("Block")}</button>
              {line(`p${p.user_id}`)}
            </li>
          ))}
        </ul>
      </section>

      {(incoming.length > 0 || outgoing.length > 0) && (
        <section className="fm-section" aria-labelledby="ppl-requests">
          <h2 id="ppl-requests" className="fm-eyebrow">{t("Meeting requests")}</h2>
          <ul className="fm-list">
            {incoming.map((r) => (
              <li key={r.id} className="fm-card fm-section">
                <p className="fm-strong">{r.other.name}</p>
                <p className="fm-small">{r.other.profession}</p>
                <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void run(`r${r.id}`, () => respond(r.id, true), "Accepted: see below where to meet.")}>{t("Accept")}</button>
                <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => void run(`r${r.id}`, () => respond(r.id, false), "Declined.")}>{t("Decline")}</button>
                {line(`r${r.id}`)}
              </li>
            ))}
            {outgoing.map((r) => (
              <li key={r.id} className="fm-card fm-section">
                <p className="fm-strong">{r.other.name}</p>
                <p className="fm-small">{t("Waiting for an answer")}</p>
                <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => void run(`r${r.id}`, () => cancelRequest(r.id), "Request withdrawn.")}>{t("Withdraw")}</button>
                {line(`r${r.id}`)}
              </li>
            ))}
          </ul>
        </section>
      )}

      {meetings.length > 0 && (
        <section className="fm-section" aria-labelledby="ppl-meetings">
          <h2 id="ppl-meetings" className="fm-eyebrow">{t("Match Corner meetings")}</h2>
          <ul className="fm-list">
            {meetings.map((r) => r.meeting && (
              <li key={r.id} className="fm-card fm-section">
                <div className="fm-row-between">
                  <p className="fm-strong">{r.other.name}</p>
                  <span className="fm-badge">{eventTime(r.meeting.starts_at, zone, language)}–{eventTime(r.meeting.ends_at, zone, language)}</span>
                </div>
                <p className="fm-small">{t("Table")} {r.meeting.table}</p>
                <ol className="fm-stack" style={{ gap: 4 }}>
                  {r.meeting.prompts.map((key) => <li key={key} className="fm-small">{promptText(key, t)}</li>)}
                </ol>
                <p className="fm-micro">{t("At the table, scan each other's QR to connect.")}</p>
                <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => void run(`r${r.id}`, () => cancelRequest(r.id), "Meeting cancelled.")}>{t("Cancel the meeting")}</button>
                {line(`r${r.id}`)}
              </li>
            ))}
          </ul>
        </section>
      )}

      {agreed.length > 0 && (
        <section className="fm-section" aria-labelledby="ppl-agreed">
          <h2 id="ppl-agreed" className="fm-eyebrow">{t("Agreed to meet")}</h2>
          <ul className="fm-list">
            {agreed.map((r) => (
              <li key={r.id} className="fm-card fm-section">
                <p className="fm-strong">{r.other.name}</p>
                <p className="fm-small">{r.other.profession}</p>
                <p className="fm-micro">{t("Find each other at the Connection Hub and scan each other's QR to connect.")}</p>
                <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => void run(`r${r.id}`, () => cancelRequest(r.id), "Meeting cancelled.")}>{t("Cancel the meeting")}</button>
                {line(`r${r.id}`)}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="fm-section" aria-labelledby="ppl-connect">
        <h2 id="ppl-connect" className="fm-eyebrow">{t("Connect")}</h2>
        <p className="fm-small">{t("Scan the other person's QR, and let them scan yours. Contacts open when both of you have scanned.")}</p>
        <QrCapture
          paused={busy}
          manualLabel="Their attendee code"
          onText={(text) => void (async () => {
            const card = await resolveCardText(text, "attendee");
            if ("error" in card) { setNotice({ tone: "warn", text: card.error, place: "scan" }); return; }
            void run("scan", () => scanPerson(card.token), (data) => scanMessage(data as ScanResult, t));
          })()}
        />
        {line("scan")}
        {connections.length > 0 && (
          <ul className="fm-list">
            {connections.map((c) => (
              <li key={c.user_id} className="fm-card fm-section">
                <p className="fm-strong">{c.name}</p>
                <p className="fm-small">{c.profession}</p>
                {c.contact.type === "LINKEDIN"
                  ? safeExternalUrl(c.contact.value) && <a className="fm-btn fm-btn-secondary fm-btn-block" href={safeExternalUrl(c.contact.value) ?? undefined} target="_blank" rel="noopener noreferrer">{t("Connect on LinkedIn")}</a>
                  : <a className="fm-btn fm-btn-secondary fm-btn-block" href={`mailto:${c.contact.value}`}>{t("Email")} {c.name.split(" ")[0]}</a>}
              </li>
            ))}
          </ul>
        )}
      </section>
      <AttendeeTabBar />
    </div>
  );
}
