"use client";

import { useCallback, useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiPost } from "@/lib/api";
import { fetchPanels, type PublicPanel } from "@/lib/competition";
import { confirmAction } from "@/lib/confirm";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

// The Grand Final check-in opens when the hall is ready, not at a time set in
// advance (FACEMARK, 8 Nov 2026): the Game Master presses this, the code on the
// Grand Final screen works for 15 minutes from then, and the final result can be
// published only after that.
export function GrandFinalOpen() {
  const { t, language } = useLanguage();
  const [final, setFinal] = useState<PublicPanel | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    const r = await fetchPanels();
    if (r.ok) setFinal(r.data.find((p) => p.kind === "grand_final") ?? null);
  }, []);
  useEffect(() => {
    let active = true;
    void fetchPanels().then((r) => { if (active && r.ok) setFinal(r.data.find((p) => p.kind === "grand_final") ?? null); });
    const tick = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => { active = false; window.clearInterval(tick); };
  }, []);

  async function open() {
    if (busy) return;
    if (!(await confirmAction(t("Open the Grand Final code now? It works for 15 minutes from this moment, and the result can be published after that.")))) return;
    setBusy(true);
    setNotice(null);
    const r = await apiPost<{ starts_at: string; ends_at: string }>("/api/staff/grand-final/open", {});
    setBusy(false);
    if (!r.ok) { setNotice({ tone: "warn", text: messageFor(r.error.code) }); return; }
    setNotice({ tone: "ok", text: `${t("The Grand Final code is open until")} ${eventTime(r.data.ends_at, final?.timezone ?? "Asia/Baku", language)}.` });
    setNow(Date.now());
    await load();
  }

  if (!final) return null;
  const opened = new Date(final.starts_at).getTime() <= now && now < new Date(final.ends_at).getTime();
  const window_ = `${eventTime(final.starts_at, final.timezone, language)}–${eventTime(final.ends_at, final.timezone, language)}`;
  return (
    <section className="fm-card fm-section" aria-labelledby="gf-open">
      <h3 id="gf-open" className="fm-strong">{t("Grand Final check-in")}</h3>
      <p className="fm-small">{t("Open the code when the hall is ready. It works for 15 minutes; until it closes the final result cannot be published.")}</p>
      <p className="fm-small" role="status">
        {opened ? t("Open now") : t("Closed")} · {window_}
      </p>
      <button type="button" className="fm-btn fm-btn-primary fm-btn-block" disabled={busy} onClick={() => void open()}>
        {t(opened ? "Restart the 15 minutes from now" : "Open the Grand Final code now (15 minutes)")}
      </button>
      {notice && <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{t(notice.text)}</p>}
    </section>
  );
}
