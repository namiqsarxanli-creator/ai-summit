"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AdminList } from "@/components/admin/AdminList";
import { AttendanceDesk } from "@/components/competition/AttendanceDesk";
import { FinalReview } from "@/components/competition/FinalReview";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { fetchVersions, type Versions } from "@/lib/final";
import { eventTime } from "@/lib/masterclasses";
import { SECTION_NEEDS, SECTION_TITLE, SECTIONS, fetchWorkspace, type OrganiserSection, type Workspace } from "@/lib/organiser";
import { fetchLiveScreen, MISSING_LABEL, MISSING_SECTION, type LiveScreen } from "@/lib/screen";
import { SetupChecklist } from "./SetupChecklist";
import { UnpaidConnections } from "./UnpaidConnections";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "forbidden" }
  | { kind: "error"; text: string }
  | { kind: "ok"; workspace: Workspace };

// The FACEMARK organiser workspace: one place for the tools the FACEMARK team
// runs on the day - scanning, Qmeter, points corrections, the final result and
// the venue screens. It adds no permission of its own: the server says which
// sections this account may use, and each section is the existing tool, whose
// endpoint checks its own role or scope again on every request.
export function OrganiserPanel() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [section, setSection] = useState<OrganiserSection>("overview");

  useEffect(() => {
    let active = true;
    void fetchWorkspace().then((r) => {
      if (!active) return;
      if (r.ok) setState({ kind: "ok", workspace: r.data });
      else if (r.error.code === "UNAUTHENTICATED") setState({ kind: "signed-out" });
      else if (r.error.code === "STAFF_NOT_AUTHORIZED") setState({ kind: "forbidden" });
      else setState({ kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in with your staff account to continue.")}</p>
        <Link href="/facemark/login" className="fm-btn fm-btn-primary fm-btn-block">{t("FACEMARK sign in")}</Link>
      </div>
    );
  }
  if (state.kind === "forbidden") {
    return (
      <div className="fm-stack">
        <p role="alert" className="fm-alert fm-alert-warn">{t("This panel is for FACEMARK organisers: administrators and Game Masters. Your own tools are on the staff page.")}</p>
        <Link href="/staff" className="fm-btn fm-btn-secondary fm-btn-block">{t("Staff page")}</Link>
      </div>
    );
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;

  const ws = state.workspace;
  const allowed = SECTIONS.filter((s) => ws.sections[s]);
  const current = ws.sections[section] ? section : "overview";
  return (
    <div className="fm-stack-lg">
      <section className="fm-staff-identity fm-stack" style={{ gap: 4 }}>
        <p className="fm-eyebrow">{t("Signed in")}</p>
        <p className="fm-h2">{ws.full_name}</p>
        <p className="fm-small">{ws.role === "ADMIN" ? t("Admin") : t(ws.game_master ? "Game Master" : "Volunteer")}</p>
      </section>

      <nav className="fm-org-nav" aria-label={t("Organiser sections")}>
        {allowed.map((s) => (
          <button key={s} type="button" aria-current={s === current ? "page" : undefined}
            className={`fm-org-tab${s === current ? " is-current" : ""}`} onClick={() => setSection(s)}>
            {t(SECTION_TITLE[s])}
          </button>
        ))}
      </nav>

      <section aria-labelledby="org-section" className="fm-stack-lg">
        <h2 id="org-section" className="fm-h3">{t(SECTION_TITLE[current])}</h2>
        {current === "overview" && <Overview ws={ws} open={setSection} />}
        {current === "scanning" && (
          <>
            <p className="fm-small">{t("Each desk shows the activity and, at a masterclass door, the entry or exit window. Scans are checked by the server; without a connection they wait on this phone.")}</p>
            <AttendanceDesk jobs="scans" />
          </>
        )}
        {current === "qmeter" && (
          <>
            <p className="fm-small">{t("Verification is manual: check the question in Qmeter yourself. This panel is not connected to the Qmeter service.")}</p>
            <AttendanceDesk jobs="qmeter" />
          </>
        )}
        {current === "corrections" && (
          <>
            <p className="fm-small">{t("The ledger itself is never edited. An award can be reversed in full, with a reason: the reversal is a new entry linked to it.")}</p>
            <AdminList kind="points-log" refreshKey={0} base="/api/staff" />
            <UnpaidConnections />
          </>
        )}
        {current === "final" && (
          <>
            <ul className="fm-small fm-org-rules">
              <li>{t("On equal points, whoever reached the total earlier is ahead.")}</li>
              <li>{t("A connection with a person the app recommended, confirmed by a mutual QR scan, earns 3 points; no meeting booking is needed.")}</li>
              <li>{t("The Founder rule is not confirmed by the organisers yet: no Founder bonus is awarded or named.")}</li>
            </ul>
            <FinalReview />
          </>
        )}
        {current === "screens" && <Screens panelCodes={ws.panel_codes} />}
      </section>
    </div>
  );
}

const STATE_TEXT: Record<LiveScreen["state"], string> = {
  not_configured: "Not set up yet",
  disabled: "Set up, but the competition ranking is off",
  open: "Open",
  review: "Closed · results in review",
  published: "Final result published",
};

function Overview({ ws, open }: { ws: Workspace; open: (s: OrganiserSection) => void }) {
  const { t, language } = useLanguage();
  const [live, setLive] = useState<{ kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: LiveScreen }>({ kind: "loading" });
  const [final, setFinal] = useState<{ kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: Versions } | null>(
    ws.sections.final ? { kind: "loading" } : null,
  );

  useEffect(() => {
    let active = true;
    void fetchLiveScreen().then((r) => {
      if (active) setLive(r.ok ? { kind: "ok", data: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    if (ws.sections.final) {
      void fetchVersions().then((r) => {
        if (active) setFinal(r.ok ? { kind: "ok", data: r.data } : { kind: "error", text: messageFor(r.error.code) });
      });
    }
    return () => { active = false; };
  }, [ws.sections.final]);

  const latest = final?.kind === "ok" ? final.data.versions[0] : undefined;
  return (
    <div className="fm-stack-lg">
      <section className="fm-card fm-section" aria-labelledby="org-competition">
        <h3 id="org-competition" className="fm-eyebrow">{t("Competition")}</h3>
        {live.kind === "loading" && <p role="status" className="fm-note">{t("Loading…")}</p>}
        {live.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t(live.text)}</p>}
        {live.kind === "ok" && (
          <>
            <p className="fm-strong">{t(STATE_TEXT[live.data.state])}</p>
            {live.data.closes_at
              ? <p className="fm-small">{t("Ranking closes at {time}").replace("{time}", eventTime(live.data.closes_at, live.data.timezone, language))}</p>
              : null}
            {live.data.missing.length > 0 && (
              <>
                <p className="fm-small">{t("The organisers still need to set:")}</p>
                <ul className="fm-small fm-org-rules">{live.data.missing.map((m) => (
                  <li key={m}>
                    {t(MISSING_LABEL[m] ?? m)}
                    {ws.role === "ADMIN" && MISSING_SECTION[m] && (
                      <> · <Link className="fm-link" href={`/admin?section=${MISSING_SECTION[m]}`}>{t("Set it in the admin console")}</Link></>
                    )}
                  </li>
                ))}</ul>
                {ws.role !== "ADMIN" && <p className="fm-small">{t("An administrator sets these in the admin console.")}</p>}
                <p className="fm-small">{t("Masterclass titles, speakers, panels, Playground booths, mentors, tables and staff are entered by the organisers; nothing is filled in for them.")}</p>
              </>
            )}
          </>
        )}
      </section>

      {ws.role === "ADMIN" && <SetupChecklist />}

      {final !== null && (
        <section className="fm-card fm-section" aria-labelledby="org-final">
          <h3 id="org-final" className="fm-eyebrow">{t("Final results")}</h3>
          {final.kind === "loading" && <p role="status" className="fm-note">{t("Loading…")}</p>}
          {final.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t(final.text)}</p>}
          {final.kind === "ok" && (
            <>
              <p className="fm-small">{t("Late scans waiting for review: {n}").replace("{n}", String(final.data.pending_offline_scans))}</p>
              <p className="fm-small">{latest
                ? t("Latest version: {v} · {status}").replace("{v}", String(latest.version)).replace("{status}", t(latest.status === "published" ? "Published" : latest.status === "draft" ? "Draft" : latest.status === "superseded" ? "Superseded" : "Discarded"))
                : t("No final snapshot yet.")}</p>
              <button type="button" className="fm-btn fm-btn-secondary" onClick={() => open("final")}>{t("Open final results")}</button>
            </>
          )}
        </section>
      )}

      <section className="fm-card fm-section" aria-labelledby="org-access">
        <h3 id="org-access" className="fm-eyebrow">{t("This account")}</h3>
        <ul className="fm-list">
          {SECTIONS.filter((s) => s !== "overview").map((s) => (
            <li key={s} className="fm-row-between fm-small">
              <span>{t(SECTION_TITLE[s])}</span>
              {ws.sections[s]
                ? <button type="button" className="fm-link" onClick={() => open(s)}>{t("Open")}</button>
                : <span className="fm-muted">{t(SECTION_NEEDS[s] ?? "Not available")}</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

const SCREENS = [
  { href: "/screen/live", title: "Live leaderboard", detail: "Top 10, latest points, the countdown to the close and shared photos." },
  { href: "/screen/final", title: "Grand Final", detail: "Only a published final result." },
  { href: "/screen/photos", title: "Beyond Now Photo", detail: "Photos both people agreed to show and a moderator approved." },
] as const;

// The hall computer's link per panel (GET /api/admin/panel-screen-links).
type ScreenLink = { id: number; title: string; kind: string; starts_at: string; timezone: string; key: string };

function Screens({ panelCodes }: { panelCodes: boolean }) {
  const { t, language } = useLanguage();
  const [panels, setPanels] = useState<{ kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: ScreenLink[] } | null>(
    panelCodes ? { kind: "loading" } : null,
  );
  useEffect(() => {
    if (!panelCodes) return;
    let active = true;
    void apiGet<ScreenLink[]>("/api/admin/panel-screen-links").then((r) => {
      if (active) setPanels(r.ok ? { kind: "ok", data: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, [panelCodes]);

  return (
    <div className="fm-stack-lg">
      <p className="fm-small">{t("Each screen opens in a new tab with no controls on it. Public screens show first name and surname initial only.")}</p>
      <ul className="fm-list">
        {SCREENS.map((s) => (
          <li key={s.href} className="fm-card fm-section">
            <p className="fm-strong">{t(s.title)}</p>
            <p className="fm-small">{t(s.detail)}</p>
            <a className="fm-btn fm-btn-secondary" href={s.href} target="_blank" rel="noopener noreferrer">{t("Open screen")}</a>
          </li>
        ))}
      </ul>
      {panels !== null && (
        <section className="fm-card fm-section" aria-labelledby="org-panels">
          <h3 id="org-panels" className="fm-eyebrow">{t("Panel QR codes (Main Stage)")}</h3>
          <p className="fm-small">{t("Open these links on the hall computer: they need no sign-in and show only that panel's codes. They stop working at midnight after the event day, or when the codes are rotated in the admin console.")}</p>
          {panels.kind === "loading" && <p role="status" className="fm-note">{t("Loading…")}</p>}
          {panels.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t(panels.text)}</p>}
          {panels.kind === "ok" && panels.data.length === 0 && <p className="fm-empty">{t("No panels are set up yet.")}</p>}
          {panels.kind === "ok" && panels.data.length > 0 && (
            <ul className="fm-list">
              {panels.data.map((p) => (
                <li key={p.id} className="fm-row-between fm-small">
                  <span>{p.title} · {eventTime(p.starts_at, p.timezone, language)}</span>
                  <a href={`/screen/panel?k=${encodeURIComponent(p.key)}`} target="_blank" rel="noopener noreferrer">{t("Open screen")}</a>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
