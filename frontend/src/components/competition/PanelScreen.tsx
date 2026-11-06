"use client";

import { useEffect, useState } from "react";

import { QrCode } from "@/components/card/QrCode";
import { LanguageSwitcher, useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

type Screen = {
  id: number; title: string; kind?: "panel" | "grand_final"; starts_at: string; ends_at: string; timezone: string;
  entry_code: string; exit_code: string; entry_short?: string; exit_short?: string;
};
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; screen: Screen };
type Phase = "entry" | "exit";

const EXIT_OPENS_MS = 10 * 60 * 1000;

/** The exit code from the panel's last 10 minutes on, the entry code before them. */
export function phaseAt(screen: Pick<Screen, "ends_at">, nowMs: number): Phase {
  return nowMs >= new Date(screen.ends_at).getTime() - EXIT_OPENS_MS ? "exit" : "entry";
}

// The Main Stage screen for one panel, filling the hall's display: the ENTRY and the EXIT code, shown on the
// big screen. Opened by its screen link (`?k=`, no sign-in: the hall computer never
// holds an administrator session) or, as before, by an administrator (`?panel=`).
// Rotating the codes in the admin panel makes both stop working.
export function PanelScreen() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  // The code follows the clock, so a busy operator cannot cost the hall its points;
  // choosing one by hand holds it there.
  const [chosen, setChosen] = useState<Phase | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams(window.location.search);
    const key = params.get("k");
    if (key && /^[1-9][0-9]{0,15}\.[0-9]{1,12}\.[0-9a-f]{64}$/.test(key)) {
      void apiGet<Screen>(`/api/screen/panel?k=${encodeURIComponent(key)}`).then((r) => {
        if (active) setState(r.ok ? { kind: "ok", screen: r.data } : { kind: "error", text: messageFor(r.error.code) });
      });
      return () => { active = false; };
    }
    const id = params.get("panel");
    if (!id || !/^[1-9][0-9]{0,15}$/.test(id)) {
      void Promise.resolve().then(() => active && setState({ kind: "error", text: "Open this page from the admin panel." }));
      return () => { active = false; };
    }
    void apiGet<Screen>(`/api/admin/panels/${id}/screen`).then((r) => {
      if (!active) return;
      setState(r.ok ? { kind: "ok", screen: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading…")}</p>;
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const { screen } = state;
  const final = screen.kind === "grand_final";  // one check-in code, no exit
  const phase = chosen ?? phaseAt(screen, now);
  const shown = final ? "entry" : phase;
  const short = shown === "entry" ? screen.entry_short : screen.exit_short;
  const name = t(final ? "Grand Final code" : shown === "entry" ? "Entry code" : "Exit code");
  return (
    <div className="fm-panel-stage">
      <div className="fm-panel-stage-qr">
        <QrCode value={shown === "entry" ? screen.entry_code : screen.exit_code} label={name} />
      </div>
      <div className="fm-panel-stage-info">
        <h2 className="fm-panel-stage-title">{screen.title}</h2>
        <p className="fm-panel-stage-time">{eventTime(screen.starts_at, screen.timezone, language)}–{eventTime(screen.ends_at, screen.timezone, language)}</p>
        <p className="fm-panel-stage-phase">{name}</p>
        {short && <p className="fm-panel-short" aria-label={t("Code to type")}>{short.slice(0, 3)} {short.slice(3)}</p>}
        <p className="fm-panel-stage-hint">{t(final ? "Scan when you enter the hall: this completes Beyond Now Founder. Open AI Summit → My points." : shown === "entry" ? "Scan to record your entry. Open AI Summit → My points." : "Scan to record your exit. Open AI Summit → My points.")}</p>
        {short && <p className="fm-panel-stage-note">{t("Camera not working? Type the six digits in AI Summit → My points → Code from the screen.")}</p>}
      </div>
      <div className="fm-panel-stage-controls">
        <LanguageSwitcher />
        {!final && (
          <div className="fm-choices" role="group" aria-label={t("Code on screen")}>
            {(["entry", "exit"] as const).map((p) => (
              <label key={p} className="fm-choice">
                <input type="radio" name="screen-phase" checked={phase === p} onChange={() => setChosen(p)} />
                {t(p === "entry" ? "Entry code (first 15 minutes)" : "Exit code (last 10 minutes)")}
              </label>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
