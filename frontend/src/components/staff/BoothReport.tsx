"use client";

import { useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Lead = { first_name: string; last_name: string; profession: string; email: string; phone: string; linkedin_url: string | null };
type Report = { booth_id: number; stats: { playground_sessions_completed: number; distinct_visitors: number; consenting_leads: number }; leads: Lead[] };
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; report: Report };

// A sponsor booth's own report: statistics, and the contacts of visitors who
// gave sponsor consent - this booth only, read live (a withdrawn consent is gone).
export function BoothReport() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    void apiGet<Report>("/api/staff/booth/report").then((r) => {
      if (active) setState(r.ok ? { kind: "ok", report: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading…")}</p>;
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const { stats, leads } = state.report;
  return (
    <div className="fm-stack-lg">
      <section className="fm-card fm-section">
        <dl className="fm-dl">
          <div><dt>{t("Playground sessions completed")}</dt><dd>{stats.playground_sessions_completed}</dd></div>
          <div><dt>{t("Visitors")}</dt><dd>{stats.distinct_visitors}</dd></div>
          <div><dt>{t("Visitors who agreed to be contacted by sponsors")}</dt><dd>{stats.consenting_leads}</dd></div>
        </dl>
      </section>
      <section className="fm-section" aria-labelledby="booth-leads">
        <h2 id="booth-leads" className="fm-eyebrow">{t("Contacts (sponsor consent given)")}</h2>
        <p className="fm-micro">{t("Use these only for this booth's follow-up. A person who withdraws consent disappears from this list.")}</p>
        {leads.length === 0 && <p className="fm-empty">{t("Nobody yet.")}</p>}
        <ul className="fm-list">
          {leads.map((l, i) => (
            <li key={`${l.email}-${i}`} className="fm-card fm-section">
              <p className="fm-strong">{l.first_name} {l.last_name}</p>
              <p className="fm-small">{l.profession}</p>
              <p className="fm-small">{l.email} · {l.phone}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
