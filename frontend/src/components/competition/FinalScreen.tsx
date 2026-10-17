"use client";

import { useEffect, useState } from "react";

import { Avatar } from "@/components/ui/Avatar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { fetchFinal, type PublicFinal } from "@/lib/final";

const REFRESH_MS = 20_000;

// The Grand Final big screen. Before a Game Master publishes, it says the result
// is under review; afterwards it shows the published version only: places, first
// name and surname initial, and each person's own photo or the robot. It never
// computes a ranking itself. It refreshes on a bounded timer while open.
export function FinalScreen() {
  const { t } = useLanguage();
  const [final, setFinal] = useState<PublicFinal | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => void fetchFinal().then((r) => {
      if (active && r.ok) setFinal(r.data);
    });
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  if (final === null) return <p role="status" className="fm-note">{t("Loading…")}</p>;
  if (final.status !== "published") {
    return (
      <p role="status" className="fm-alert fm-alert-info">
        {t(final.status === "open" ? "The competition is still open." : "The ranking has closed. The Game Master is reviewing the results.")}
      </p>
    );
  }
  return (
    <div className="fm-stack-lg">
      <section className="fm-section" aria-labelledby="final-top">
        <h2 id="final-top" className="fm-eyebrow">{t("Final ranking")}</h2>
        <ol className="fm-list">
          {final.top.map((p) => (
            <li key={p.place} className="fm-card fm-row-between" data-testid={`final-${p.place}`}>
              <span className="fm-row" style={{ gap: 12, alignItems: "center" }}>
                <span className="fm-h2" aria-label={t("Place {n}").replace("{n}", String(p.place))}>{p.place}</span>
                <Avatar url={p.photo_url} size="rank" />
                <span className="fm-strong">{p.name}</span>
              </span>
              <span className="fm-badge">{p.points}</span>
            </li>
          ))}
        </ol>
      </section>
      <section className="fm-section" aria-labelledby="final-founders">
        <h2 id="final-founders" className="fm-eyebrow">{t("Beyond Now Founders · Lucky Draw")}</h2>
        {final.founders.length === 0
          ? <p className="fm-empty">{t(final.founder_rule_confirmed === false ? "The Founder list appears once the organisers confirm its rule." : "Nobody completed all four zones.")}</p>
          : <ul className="fm-chips">{final.founders.map((f, i) => <li key={`${f.name}-${i}`} className="fm-chip">{f.name}</li>)}</ul>}
      </section>
      <p className="fm-micro">{t("Result version {n}").replace("{n}", String(final.version))}</p>
    </div>
  );
}
