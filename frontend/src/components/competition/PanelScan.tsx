"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import {
  CATEGORY_LABEL,
  OUTCOME_TEXT,
  fetchMyCompetition,
  fetchPanels,
  isPanelCode,
  panelCodeFrom,
  scanFailure,
  scanPanel,
  type MyCompetition,
  type PublicPanel,
} from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { ZONE_LABEL } from "@/lib/final";
import { eventTime } from "@/lib/masterclasses";

import { QrCapture } from "./QrCapture";

type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "ok"; mine: MyCompetition; panels: PublicPanel[] } | { kind: "error"; text: string };
type Notice = { tone: "ok" | "warn"; text: string } | null;

function fill(text: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce((out, [key, value]) => out.replace(`{${key}}`, String(value)), text);
}

// The attendee's competition page: own points per category, and the Main Stage
// scan - the panel's code earns the panel while the panel runs (one code, no exit and
// no 15-minute limit since 9 Nov 2026). The server checks the code and the time; this page only shows its answer.
export function PanelScan() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const apply = useCallback((mine: ApiResult<MyCompetition>, panels: ApiResult<PublicPanel[]>) => {
    if (!mine.ok) {
      setState(mine.error.code === "UNAUTHENTICATED" || mine.error.code === "REGISTRATION_NOT_VERIFIED"
        ? { kind: "signed-out" }
        : { kind: "error", text: messageFor(mine.error.code) });
      return;
    }
    setState({ kind: "ok", mine: mine.data, panels: panels.ok ? panels.data : [] });
  }, []);
  const load = useCallback(
    () => Promise.all([fetchMyCompetition(), fetchPanels()]).then(([m, p]) => apply(m, p)),
    [apply],
  );
  // Opened from the screen's QR with a phone's camera (/points?code=...): the code is
  // sent once, as if scanned here, and taken out of the address so a reload sends nothing.
  const sendRef = useRef<((text: string) => Promise<void>) | null>(null);
  useEffect(() => {
    let active = true;
    const linked = new URLSearchParams(window.location.search).get("code");
    if (linked) window.history.replaceState(null, "", "/points");
    void Promise.all([fetchMyCompetition(), fetchPanels()]).then(([m, p]) => {
      if (!active) return;
      apply(m, p);
      if (linked && m.ok) void sendRef.current?.(linked);
    });
    return () => { active = false; };
  }, [apply]);
  useEffect(() => { sendRef.current = send; });

  async function send(scanned: string) {
    if (inFlight.current) return;
    const text = panelCodeFrom(scanned);
    if (!isPanelCode(text)) {
      setNotice({ tone: "warn", text: "This is not a panel code. Scan the code on the main screen." });
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const result = await scanPanel(text);
      if (!result.ok) setNotice({ tone: "warn", text: scanFailure(result, messageFor) });
      else if (result.data.grand_final) setNotice({ tone: "ok", text: "Grand Final recorded. Stay for the Lucky Draw." });
      else if (result.data.award) setNotice({ tone: result.data.award.points > 0 ? "ok" : "warn", text: OUTCOME_TEXT[result.data.award.outcome] });
      else setNotice({ tone: "ok", text: "Entry recorded." });
      await load();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading your points…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <section className="fm-stack">
        <p className="fm-alert fm-alert-info">{t("Sign in to scan panel codes and see your points.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
      </section>
    );
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;

  const { mine, panels } = state;
  return (
    <div className="fm-stack-lg">
      {!mine.eligible && <p className="fm-alert fm-alert-info">{t("You take part in the event without competing for points.")}</p>}
      <section className="fm-card fm-section" aria-labelledby="comp-points">
        <h2 id="comp-points" className="fm-eyebrow">{t("My points")}</h2>
        <p className="fm-h2">{fill(t("{n} of {total}"), { n: mine.total, total: mine.maximum })}</p>
        <dl className="fm-dl">
          {mine.categories.map((c) => (
            <div key={c.category}>
              <dt>{t(CATEGORY_LABEL[c.category] ?? c.category)}</dt>
              <dd>
                {fill(t("{n} of {total}"), { n: c.points, total: c.maximum })}
                {c.points >= c.maximum && <span className="fm-done" role="img" aria-label={t("Completed")}> ✓</span>}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {mine.final_status === "pending_review" && (
        <p role="status" className="fm-alert fm-alert-info">{t("The ranking has closed. The Game Master is reviewing the results.")}</p>
      )}
      {mine.final && (
        <p role="status" className="fm-alert fm-alert-ok">
          {mine.final.place !== null
            ? fill(t("Final result: place {n} with {p} points."), { n: mine.final.place, p: mine.final.points ?? 0 })
            : t("The final result is published.")}
        </p>
      )}
      {mine.zones && (
        <section className="fm-card fm-section" aria-labelledby="comp-founder">
          <h2 id="comp-founder" className="fm-eyebrow">{t("Beyond Now Founder")}</h2>
          <p className="fm-small">{t("A scored activity in all four zones and the Grand Final scan: +15 points and a place in the Lucky Draw.")}</p>
          <Link className="fm-link fm-small" href="/guide">{t("How to play")}</Link>
          {mine.founder_rule_confirmed === false && <p className="fm-micro">{t("The organisers are still confirming the Founder rule; the bonus is added once it is confirmed.")}</p>}
          <ul className="fm-stack" style={{ gap: 4 }}>
            {Object.entries(mine.zones).map(([zone, done]) => (
              <li key={zone} className="fm-small" data-done={done ? "true" : "false"}>
                {done ? "✓" : "○"} {t(ZONE_LABEL[zone] ?? zone)}
              </li>
            ))}
          </ul>
          {mine.founder && <p className="fm-strong">{t(mine.lucky_draw ? "You are a Beyond Now Founder and in the Lucky Draw." : "You are a Beyond Now Founder.")}</p>}
        </section>
      )}

      <section className="fm-section" aria-labelledby="comp-panels">
        <h2 id="comp-panels" className="fm-eyebrow">{t("Main Stage panels")}</h2>
        <p className="fm-small">{t("Scan the code on the big screen at any time during the panel: the scan earns its points.")}</p>
        {panels.length > 0 && (
          <ul className="fm-list">
            {panels.map((p) => (
              <li key={p.id} className="fm-small">
                <span className="fm-strong">{p.title}</span> · {eventTime(p.starts_at, p.timezone, language)}–{eventTime(p.ends_at, p.timezone, language)}
                {p.kind === "grand_final" && <> · {t("scan at the hall entrance (Beyond Now Founder)")}</>}
              </li>
            ))}
          </ul>
        )}
        <QrCapture paused={busy} onText={(text) => void send(text)} manualLabel="Code from the screen" />
        {notice && (
          <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
            {t(notice.text)}
          </p>
        )}
      </section>
      <AttendeeTabBar />
    </div>
  );
}
