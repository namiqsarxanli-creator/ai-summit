"use client";

import Link from "next/link";
import { useLanguage } from "@/components/ui/LanguageProvider";
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

  // Signed in: nothing here. The attendee's identity lives on the Profile tab of
  // the tab bar; Home only offers the ways in to someone who is not signed in.
  if (state.kind === "signed-in") return null;
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
      {/* Staff (/staff/login) and organisers (/admin) are not linked from here:
          their own links are handed to them, and only their accounts sign in. */}
    </div>
  );
}
