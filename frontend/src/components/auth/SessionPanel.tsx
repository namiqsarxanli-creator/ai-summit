"use client";

import Link from "next/link";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { HomeIcon } from "@/components/ui/HomeIcons";
import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { fetchSession, type SessionState } from "@/lib/session";

// M08 gives the signed-in attendee a proper tab bar (Home · Booths ·
// My QR · Leaderboard · Profile — see AttendeeTabBar, rendered by
// HomeDashboard below this panel on "/"), which supersedes the ad hoc
// quick-link buttons this component used to carry on its own — two
// separate ways to the same two destinations is exactly the
// "disconnected pages" feel M08 replaces, not a redesign of what this
// component is FOR (identity summary + sign out, unchanged).

export function SessionPanel() {
  const { t } = useLanguage();
  const [state, setState] = useState<SessionState>({ kind: "loading" });
  useEffect(() => {
    let cancelled = false;
    fetchSession().then((next) => {
      if (!cancelled) setState(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") {
    return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  }

  if (state.kind === "signed-in") {
    const { attendee } = state;
    return (
      <section className="fm-session-strip" aria-label={t("Signed in")}>
        <Link href="/profile" className="fm-session-avatar" aria-label={t("Profile")}>
          <HomeIcon name="profile" />
        </Link>
        <div className="fm-session-identity">
          <p className="fm-session-name">{attendee.first_name} {attendee.last_name}</p>
          <p className="fm-session-profession">{attendee.profession}</p>
        </div>
      </section>
    );
  }
  return (
    <div className="fm-stack">
      {state.kind === "error" && (
        <p role="alert" className="fm-alert fm-alert-warn">
          {t(messageFor(state.error.code))}
        </p>
      )}
      <Link href="/register" className="fm-btn fm-btn-primary fm-btn-block">
        {t("Register as an attendee")}
      </Link>
      <Link href="/login" className="fm-btn fm-btn-secondary fm-btn-block">
        {t("Already registered? Sign in")}
      </Link>
      {state.kind === "signed-out" && <Link href="/staff/login" className="fm-staff-entry">{t("Staff sign in")} <span aria-hidden="true">→</span></Link>}
    </div>
  );
}
