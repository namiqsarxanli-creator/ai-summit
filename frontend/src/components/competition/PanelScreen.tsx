"use client";

import { useEffect, useState } from "react";

import { QrCode } from "@/components/card/QrCode";
import { LanguageSwitcher, useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { panelScanUrl } from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

type Screen = {
  id: number; title: string; kind?: "panel" | "grand_final"; starts_at: string; ends_at: string; timezone: string;
  entry_code: string; entry_short?: string;
};
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; screen: Screen };

// The Main Stage screen for one panel, filling the hall's display: its one code, the
// entry code (FACEMARK, 9 Nov 2026: no exit code; the entry scan earns the panel, and
// the QR is taken down at the end). Opened by its screen link (`?k=`, no sign-in: the
// hall computer never holds an administrator session) or, as before, by an
// administrator (`?panel=`). Rotating the codes in the admin panel makes it stop working.
export function PanelScreen() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

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
  const final = screen.kind === "grand_final";
  const short = screen.entry_short;
  const name = t(final ? "Grand Final code" : "Entry code");
  return (
    <div className="fm-panel-stage">
      <div className="fm-panel-stage-qr">
        {/* A link, so a phone's own camera opens My points with the code (lib/competition.ts). */}
        <QrCode value={panelScanUrl(screen.entry_code, window.location.origin)} label={name} />
      </div>
      <div className="fm-panel-stage-info">
        <h2 className="fm-panel-stage-title">{screen.title}</h2>
        <p className="fm-panel-stage-time">{eventTime(screen.starts_at, screen.timezone, language)}–{eventTime(screen.ends_at, screen.timezone, language)}</p>
        <p className="fm-panel-stage-phase">{name}</p>
        {short && <p className="fm-panel-short" aria-label={t("Code to type")}>{short.slice(0, 3)} {short.slice(3)}</p>}
        <p className="fm-panel-stage-hint">{t(final ? "Scan when you enter the hall: this completes Beyond Now Founder. Open AI Summit → My points." : "Scan at any time during the panel to earn its points. Open AI Summit → My points.")}</p>
        {short && <p className="fm-panel-stage-note">{t("Camera not working? Type the six digits in AI Summit → My points → Code from the screen.")}</p>}
      </div>
      <div className="fm-panel-stage-controls">
        <LanguageSwitcher />
      </div>
    </div>
  );
}
