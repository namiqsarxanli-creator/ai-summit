"use client";

import { useCallback, useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Level = "ok" | "warn" | "crit" | "skip";
type Check = { name: string; level: Level; detail: string };
type SectionCount = { last_15_min: number; last_hour: number; last_day: number; last_at: number; codes: Record<string, number> };
type ErrorEvent = { at: number; section: string; method: string; path: string; status: number; code: string };
export type SystemStatusReport = {
  status: "ok" | "warn" | "crit";
  checks: Check[];
  admission: { limit: number; inside: number; waiting: number; turned_away: number };
  errors: { started_at: number; now: number; total_since_start: number; sections: Record<string, SectionCount>; recent: ErrorEvent[] };
};

// Every part of the site the error log names (app/core/error_log.py SECTIONS).
const SECTION_LABELS: Record<string, string> = {
  registration: "Registration",
  sign_in: "Attendee sign-in",
  staff_sign_in: "Staff sign-in",
  masterclasses: "Masterclasses",
  playground: "AI Playground",
  networking: "Networking and recommendations",
  photos: "Photos",
  mentors: "Meet the Mentor",
  panels: "Panels and Qmeter",
  leaderboard: "Leaderboard and points",
  screens: "Hall screens",
  final: "Final results",
  scanning: "Scanning",
  booths: "Booths",
  staff: "Staff tools",
  admin: "Admin console",
  live_updates: "Live updates",
  cards: "Networking cards",
  profile: "Profile",
  health: "Health check",
  other: "Other",
};
// What each server error code means, in plain words.
const CODE_LABELS: Record<string, string> = {
  SERVER_BUSY: "Server busy (too many requests at once)",
  MAINTENANCE: "Briefly unavailable (database lock)",
  INTERNAL_ERROR: "Internal error (a bug or a failing dependency)",
  NO_ERROR_CODE: "Error without a code",
};
const CHECK_LABELS: Record<string, string> = {
  database: "Database",
  migrations: "Database schema",
  "outbox-backlog": "Sign-in code emails: queue",
  "outbox-failures": "Sign-in code emails: failures",
  outbox: "Sign-in code emails",
  "media-write": "Photo storage",
  "media-space": "Disk space",
  media: "Photo storage",
};
const LEVEL: Record<Level | "ok", { label: string; cls: string }> = {
  ok: { label: "Working", cls: "fm-badge-ok" },
  warn: { label: "Warning", cls: "fm-badge-warn" },
  crit: { label: "Not working", cls: "fm-badge-crit" },
  skip: { label: "Not checked", cls: "" },
};
const REFRESH_MS = 30_000;

function clock(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function duration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

// For the system's operator (AIWorks) only: the admin console, never the
// organiser panel. Refreshes every 30 seconds while open.
export function SystemStatus() {
  const { t } = useLanguage();
  const [report, setReport] = useState<SystemStatusReport | null>(null);
  const [error, setError] = useState("");
  const [loadedAt, setLoadedAt] = useState<number | null>(null);

  const load = useCallback(async () => {
    const r = await apiGet<SystemStatusReport>("/api/admin/system-status");
    if (r.ok) {
      setReport(r.data);
      setError("");
      setLoadedAt(Date.now());
    } else {
      // The page itself failing is the loudest signal: keep the last report.
      setError(messageFor(r.error.code));
    }
  }, []);

  useEffect(() => {
    let active = true;
    const tick = () => { if (active) void load(); };
    tick();
    const timer = window.setInterval(tick, REFRESH_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);

  if (!report) {
    return error
      ? <p role="alert" className="fm-alert fm-alert-warn">{t(error)}</p>
      : <p role="status" className="text-sm text-(--fm-ink-soft)">{t("Loading…")}</p>;
  }
  const overall = LEVEL[report.status];
  const sections = Object.entries(report.errors.sections).sort((a, b) => b[1].last_day - a[1].last_day);
  return (
    <div className="grid gap-5">
      {error && <p role="alert" className="fm-alert fm-alert-warn">{t("The status could not be refreshed; the last report is shown.")} {t(error)}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <span className={`fm-badge ${overall.cls}`} aria-label={t("Overall state")}>{t(overall.label)}</span>
        <span className="text-sm text-(--fm-ink-soft)">
          {t("Server running for")} {duration(report.errors.now - report.errors.started_at)}
          {loadedAt !== null && <> · {t("Checked at")} {new Date(loadedAt).toLocaleTimeString()}</>}
        </span>
      </div>

      <section aria-label={t("Parts of the system")}>
        <h3 className="mb-2 font-semibold">{t("Parts of the system")}</h3>
        <ul className="grid gap-2">
          {report.checks.map((c) => (
            <li key={c.name} className="flex flex-wrap items-baseline gap-2">
              <span className={`fm-badge ${LEVEL[c.level].cls}`}>{t(LEVEL[c.level].label)}</span>
              <strong>{t(CHECK_LABELS[c.name] ?? c.name)}</strong>
              <span className="text-sm text-(--fm-ink-soft)">{c.detail}</span>
            </li>
          ))}
          <li className="flex flex-wrap items-baseline gap-2">
            <span className={`fm-badge ${report.admission.waiting > 0 ? "fm-badge-warn" : "fm-badge-ok"}`}>
              {t(report.admission.waiting > 0 ? "Warning" : "Working")}
            </span>
            <strong>{t("Server load")}</strong>
            <span className="text-sm text-(--fm-ink-soft)">
              {t("in progress")} {report.admission.inside}/{report.admission.limit} · {t("waiting")} {report.admission.waiting} · {t("turned away since start")} {report.admission.turned_away}
            </span>
          </li>
        </ul>
      </section>

      <section aria-label={t("Server errors by section")}>
        <h3 className="mb-2 font-semibold">{t("Server errors by section")}</h3>
        {sections.length === 0
          ? <p className="text-sm">{t("No server errors since the server started.")}</p>
          : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left">
                  <th>{t("Section")}</th><th>{t("15 min")}</th><th>{t("1 hour")}</th><th>{t("24 hours")}</th><th>{t("Last")}</th><th>{t("Why")}</th>
                </tr>
              </thead>
              <tbody>
                {sections.map(([key, s]) => (
                  <tr key={key} className={s.last_15_min > 0 ? "font-semibold text-(--fm-red)" : undefined}>
                    <td>{t(SECTION_LABELS[key] ?? key)}</td>
                    <td>{s.last_15_min}</td>
                    <td>{s.last_hour}</td>
                    <td>{s.last_day}</td>
                    <td>{clock(s.last_at)}</td>
                    <td>{Object.entries(s.codes).map(([code, n]) => `${t(CODE_LABELS[code] ?? code)} × ${n}`).join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </section>

      {report.errors.recent.length > 0 && (
        <section aria-label={t("Latest server errors")}>
          <h3 className="mb-2 font-semibold">{t("Latest server errors")}</h3>
          <ol className="grid gap-1 text-sm">
            {report.errors.recent.slice(0, 50).map((e, i) => (
              <li key={`${e.at}-${i}`}>
                <time>{clock(e.at)}</time> · {t(SECTION_LABELS[e.section] ?? e.section)} · {t(CODE_LABELS[e.code] ?? e.code)} · <code>{e.method} {e.path}</code> ({e.status})
              </li>
            ))}
          </ol>
        </section>
      )}
      <p className="text-sm text-(--fm-ink-soft)">{t("If this page does not open at all, the server itself is down: the alerts outside the site report that.")}</p>
    </div>
  );
}
