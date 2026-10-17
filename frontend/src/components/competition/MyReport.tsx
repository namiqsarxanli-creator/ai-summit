"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { safeExternalUrl } from "@/lib/card";
import { CATEGORY_LABEL, type CategoryTotal } from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { ZONE_LABEL } from "@/lib/final";
import { eventTime } from "@/lib/masterclasses";
import { safePhotoUrl, type PairPhoto } from "@/lib/pairPhotos";
import type { ConnectionRow } from "@/lib/people";
import type { ProgrammeItem } from "@/components/programme/MyProgramme";

type Report = {
  eligible: boolean;
  total: number;
  categories: CategoryTotal[];
  zones: Record<string, boolean>;
  founder: boolean;
  lucky_draw: boolean;
  final: { version: number; place: number | null; points: number | null; founder: boolean } | null;
  final_status: "open" | "pending_review" | "published";
  timezone: string;
  attended: ProgrammeItem[];
  panels: { title: string; starts_at: string; ends_at: string }[];
  connections: ConnectionRow[];
  photos: PairPhoto[];
};
type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "error"; text: string } | { kind: "ok"; report: Report };

// After the event: one's own score, place, Founder status, what one verifiably
// attended, the people one connected with and the shared photos.
export function MyReport() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    void apiGet<Report>("/api/attendees/me/report").then((r) => {
      if (!active) return;
      if (r.ok) setState({ kind: "ok", report: r.data });
      else if (r.error.code === "UNAUTHENTICATED" || r.error.code === "REGISTRATION_NOT_VERIFIED") setState({ kind: "signed-out" });
      else setState({ kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading your report…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <section className="fm-stack">
        <p className="fm-alert fm-alert-info">{t("Sign in to see your report.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
      </section>
    );
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const r = state.report;
  const zone = r.timezone;
  return (
    <div className="fm-stack-lg">
      <section className="fm-card fm-section" aria-labelledby="rep-score">
        <h2 id="rep-score" className="fm-eyebrow">{t("My points")}</h2>
        <p className="fm-h2">{r.total} / 100</p>
        {r.final && r.final.place !== null && <p className="fm-strong">{t("Final result: place {n} with {p} points.").replace("{n}", String(r.final.place)).replace("{p}", String(r.final.points ?? 0))}</p>}
        {r.final_status === "pending_review" && <p className="fm-small">{t("The ranking has closed. The Game Master is reviewing the results.")}</p>}
        {r.founder && <p className="fm-strong">{t(r.lucky_draw ? "You are a Beyond Now Founder and in the Lucky Draw." : "You are a Beyond Now Founder.")}</p>}
        <dl className="fm-dl">
          {r.categories.map((c) => (
            <div key={c.category}><dt>{t(CATEGORY_LABEL[c.category] ?? c.category)}</dt><dd>{c.points} / {c.maximum}</dd></div>
          ))}
        </dl>
        <ul className="fm-stack" style={{ gap: 4 }}>
          {Object.entries(r.zones).map(([z, done]) => <li key={z} className="fm-small">{done ? "✓" : "○"} {t(ZONE_LABEL[z] ?? z)}</li>)}
        </ul>
      </section>

      <section className="fm-section" aria-labelledby="rep-history">
        <h2 id="rep-history" className="fm-eyebrow">{t("What I attended")}</h2>
        {r.attended.length === 0 && r.panels.length === 0 && <p className="fm-empty">{t("Nothing verified yet.")}</p>}
        <ul className="fm-list">
          {r.panels.map((p) => <li key={p.starts_at} className="fm-small">{eventTime(p.starts_at, zone, language)} · {p.title}</li>)}
          {r.attended.map((i) => <li key={`${i.kind}-${i.id}`} className="fm-small">{eventTime(i.starts_at, zone, language)} · {i.kind === "playground" ? t(i.title) : i.title}</li>)}
        </ul>
      </section>

      <section className="fm-section" aria-labelledby="rep-people">
        <h2 id="rep-people" className="fm-eyebrow">{t("People I met")}</h2>
        {r.connections.length === 0 && <p className="fm-empty">{t("No connections yet.")}</p>}
        <ul className="fm-list">
          {r.connections.map((c) => (
            <li key={c.user_id} className="fm-card fm-section">
              <p className="fm-strong">{c.name}</p>
              {c.contact.type === "LINKEDIN"
                ? safeExternalUrl(c.contact.value) && <a href={safeExternalUrl(c.contact.value) ?? undefined} target="_blank" rel="noopener noreferrer">{t("Connect on LinkedIn")}</a>
                : <a href={`mailto:${c.contact.value}`}>{c.contact.value}</a>}
            </li>
          ))}
        </ul>
      </section>

      {r.photos.length > 0 && (
        <section className="fm-section" aria-labelledby="rep-photos">
          <h2 id="rep-photos" className="fm-eyebrow">{t("Beyond Now Photo")}</h2>
          <ul className="fm-chips">
            {r.photos.map((p) => {
              const src = safePhotoUrl(p.photo_url);
              // eslint-disable-next-line @next/next/no-img-element -- a server-made, fixed-shape URL
              return src ? <li key={p.id}><img src={src} alt={t("Photo with {name}").replace("{name}", p.with)} width={160} height={160} style={{ borderRadius: 12 }} /></li> : null;
            })}
          </ul>
        </section>
      )}
      <AttendeeTabBar />
    </div>
  );
}
