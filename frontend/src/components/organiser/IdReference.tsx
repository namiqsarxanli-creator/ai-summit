"use client";

import { useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";

type Row = { id: number; name: string };
type Reference = { booths: Row[]; staff: Row[] };

// The numbers the points list filters by: which booth and which staff account each
// one is. Names only, no contact data (GET /api/staff/id-reference, organisers).
export function IdReference() {
  const { t } = useLanguage();
  const [data, setData] = useState<Reference | null>(null);
  useEffect(() => {
    let active = true;
    void apiGet<Reference>("/api/staff/id-reference").then((r) => { if (active && r.ok) setData(r.data); });
    return () => { active = false; };
  }, []);
  if (!data) return null;
  return (
    <details className="fm-card fm-section">
      <summary className="fm-strong">{t("Booth and staff IDs")}</summary>
      <h4 className="fm-eyebrow">{t("Booth ID")}</h4>
      <ul className="fm-list" style={{ gap: 4 }}>
        {data.booths.map((b) => <li key={b.id} className="fm-row-between fm-small"><span>{t(b.name)}</span><span className="fm-strong">{b.id}</span></li>)}
      </ul>
      <h4 className="fm-eyebrow">{t("Staff ID")}</h4>
      <ul className="fm-list" style={{ gap: 4 }}>
        {data.staff.map((s) => <li key={s.id} className="fm-row-between fm-small"><span>{s.name}</span><span className="fm-strong">{s.id}</span></li>)}
      </ul>
    </details>
  );
}
