"use client";

import { useEffect, useState } from "react";

import { QrCode } from "@/components/card/QrCode";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

type Screen = { id: number; title: string; starts_at: string; ends_at: string; timezone: string; entry_code: string; exit_code: string };
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; screen: Screen };

// The Main Stage screen for one panel: the ENTRY and the EXIT code, shown on the
// big screen by an administrator's browser. The codes come only from the admin
// API; rotating them in the admin panel makes these stop working.
export function PanelScreen() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [phase, setPhase] = useState<"entry" | "exit">("entry");

  useEffect(() => {
    let active = true;
    const id = new URLSearchParams(window.location.search).get("panel");
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
  return (
    <div className="fm-stack-lg">
      <h2 className="fm-h2">{screen.title}</h2>
      <p className="fm-text-soft">{eventTime(screen.starts_at, screen.timezone, language)}–{eventTime(screen.ends_at, screen.timezone, language)}</p>
      <div className="fm-choices" role="group" aria-label={t("Code on screen")}>
        {(["entry", "exit"] as const).map((p) => (
          <label key={p} className="fm-choice">
            <input type="radio" name="screen-phase" checked={phase === p} onChange={() => setPhase(p)} />
            {t(p === "entry" ? "Entry code (first 15 minutes)" : "Exit code (last 10 minutes)")}
          </label>
        ))}
      </div>
      <div className="fm-card" style={{ maxWidth: 560 }}>
        <QrCode value={phase === "entry" ? screen.entry_code : screen.exit_code} label={t(phase === "entry" ? "Entry code" : "Exit code")} />
      </div>
      <p className="fm-text">{t(phase === "entry" ? "Scan to record your entry. Open AI Summit → My points." : "Scan to record your exit. Open AI Summit → My points.")}</p>
    </div>
  );
}
