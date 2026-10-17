"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

export type ProgrammeItem = {
  kind: "masterclass" | "playground" | "meeting" | "mentor";
  id: number;
  title: string;
  place: string | null;
  starts_at: string;
  ends_at: string;
  status: string;
  conflict: boolean;
};
type Programme = { timezone: string; items: ProgrammeItem[] };
type State = { kind: "loading" } | { kind: "signed-out" } | { kind: "error"; text: string } | { kind: "ok"; data: Programme };

const KIND_LABEL: Record<ProgrammeItem["kind"], string> = {
  masterclass: "Masterclass",
  playground: "AI Playground",
  meeting: "Match Corner meeting",
  mentor: "Meet the Mentor",
};
const KIND_LINK: Record<ProgrammeItem["kind"], string> = {
  masterclass: "/masterclasses",
  playground: "/playground",
  meeting: "/people",
  mentor: "/people",
};
const STATUS_LABEL: Record<string, string> = {
  confirmed: "Confirmed",
  waitlisted: "On the waiting list",
  checked_in: "Arrived",
  completed: "Completed",
  entered: "Entered",
  attended: "Attended",
  scheduled: "Confirmed",
};

// The attendee's own day in event time: what they booked, where they wait and
// what they have done. The server builds it from the real bookings.
export function MyProgramme() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let active = true;
    void apiGet<Programme>("/api/attendees/me/programme").then((r) => {
      if (!active) return;
      if (r.ok) setState({ kind: "ok", data: r.data });
      else if (r.error.code === "UNAUTHENTICATED" || r.error.code === "REGISTRATION_NOT_VERIFIED") setState({ kind: "signed-out" });
      else setState({ kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading your programme…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <section className="fm-stack">
        <p className="fm-alert fm-alert-info">{t("Sign in to see your programme.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
      </section>
    );
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const { items, timezone } = state.data;
  return (
    <div className="fm-stack-lg">
      {items.length === 0 ? (
        <p className="fm-empty">{t("Nothing booked yet. Choose masterclasses, Playground sessions and mentor groups.")}</p>
      ) : (
        <ol className="fm-list">
          {items.map((i) => (
            <li key={`${i.kind}-${i.id}`} className="fm-card fm-section" data-testid={`prog-${i.kind}-${i.id}`}>
              <div className="fm-row-between">
                <span className="fm-badge">{eventTime(i.starts_at, timezone, language)}–{eventTime(i.ends_at, timezone, language)}</span>
                <span className="fm-micro">{t(STATUS_LABEL[i.status] ?? i.status)}</span>
              </div>
              <p className="fm-eyebrow">{t(KIND_LABEL[i.kind])}</p>
              <p className="fm-strong">{i.kind === "playground" ? t(i.title) : i.title}</p>
              {i.place && <p className="fm-small">{i.kind === "meeting" ? `${t("Table")} ${i.place}` : i.place}</p>}
              {i.conflict && <p role="alert" className="fm-alert fm-alert-warn">{t("This overlaps another item of your programme. Ask the organisers.")}</p>}
              <Link href={KIND_LINK[i.kind]} className="fm-btn fm-btn-quiet">{t("Details")} →</Link>
            </li>
          ))}
        </ol>
      )}
      <AttendeeTabBar />
    </div>
  );
}
