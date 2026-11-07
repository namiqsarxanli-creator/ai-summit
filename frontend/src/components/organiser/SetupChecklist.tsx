"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Check = { key: string; status: "ok" | "missing" | "info"; detail: string };
type Readiness = { ready: boolean; checks: Check[] };
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: Readiness };

// Each readiness check (backend competition/readiness.py), what it is, and the
// section of the existing admin console where an administrator sets it.
export const SETUP: Record<string, { label: string; section: string | null }> = {
  event: { label: "Event and its date", section: "masterclasses" },
  masterclass_blocks: { label: "Masterclass blocks", section: "masterclasses" },
  masterclass_rooms: { label: "Masterclass rooms", section: "masterclasses" },
  masterclasses_published: { label: "Published masterclasses", section: "masterclasses" },
  masterclass_schedule: { label: "Masterclass times", section: "masterclasses" },
  panels: { label: "Main Stage panels", section: "competition" },
  panel_schedule: { label: "Panel times", section: "competition" },
  grand_final: { label: "Grand Final check-in", section: "competition" },
  competition_close: { label: "Competition close (17:30)", section: "competition" },
  competition_mode: { label: "Competition ranking switched on", section: "competition" },
  playground_slots: { label: "AI Playground booths and slots", section: "playground" },
  playground_schedule: { label: "Playground slot times", section: "playground" },
  playground_booth_staff: { label: "Booth staff of Playground booths", section: "staff" },
  mentor_sessions: { label: "Mentor groups", section: null },
  mentor_schedule: { label: "Mentor group times", section: null },
  mentor_hosts: { label: "Mentor group hosts (scopes)", section: "competition" },
  match_corner_tables: { label: "Match Corner tables", section: null },
  masterclass_door_scopes: { label: "Masterclass door volunteers (scopes)", section: "competition" },
  game_masters: { label: "Game Masters (scopes)", section: "competition" },
  competition_exclusions: { label: "Who does not compete", section: "competition" },
  founder_rule: { label: "Founder rule", section: null },
  reminders: { label: "Reminder emails", section: "competition" },
};

const STATUS: Record<Check["status"], string> = { ok: "Done", missing: "Missing", info: "For information" };

// The administrator's setup list on the FACEMARK overview: the server's own
// read-only readiness checks, each linked to the admin console section that sets
// it. Nothing is filled in here - a missing value stays missing until entered.
export function SetupChecklist() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  useEffect(() => {
    let active = true;
    void apiGet<Readiness>("/api/admin/readiness").then((r) => {
      if (active) setState(r.ok ? { kind: "ok", data: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  return (
    <section className="fm-card fm-section" aria-labelledby="org-setup">
      <h3 id="org-setup" className="fm-eyebrow">{t("Event setup")}</h3>
      <p className="fm-small">{t("Points can be earned only once the competition close is set; the ranking is shown once the competition ranking is switched on. Each activity also needs its own setup below.")}</p>
      {state.kind === "loading" && <p role="status" className="fm-note">{t("Loading…")}</p>}
      {state.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>}
      {state.kind === "ok" && (
        <>
          <p className="fm-strong">{t(state.data.ready ? "Every required item is set." : "Not ready: the missing items below still need the organisers' data.")}</p>
          <ul className="fm-list" aria-label={t("Event setup")}>
            {state.data.checks.map((c) => {
              const item = SETUP[c.key];
              return (
                <li key={c.key} className="fm-small" data-status={c.status}>
                  <span className="fm-strong">{c.status === "ok" ? "✓" : c.status === "missing" ? "○" : "i"} {t(item?.label ?? c.key)}</span>
                  {" · "}{t(STATUS[c.status])}
                  <br />
                  <span className="fm-muted">{c.detail}</span>
                  {item?.section && c.status !== "ok" && (
                    <> · <Link className="fm-link" href={`/admin?section=${item.section}`}>{t("Open in the admin console")}</Link></>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
