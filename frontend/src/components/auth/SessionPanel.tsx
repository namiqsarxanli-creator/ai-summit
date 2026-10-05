"use client";

import Link from "next/link";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { HomeIcon } from "@/components/ui/HomeIcons";
import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { fetchSession, logout, type SessionState } from "@/lib/session";

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
  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSession().then((next) => {
      if (!cancelled) setState(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSignOut() {
    setSigningOut(true);
    setLogoutError(null);
    const result = await logout();
    if (result.ok) {
      // Ask the server rather than assume: the session is revoked
      // server-side, so this must now come back signed-out.
      setState(await fetchSession());
    } else {
      setLogoutError(t(messageFor(result.error.code)));
    }
    setSigningOut(false);
  }

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
        <button
          type="button"
          onClick={onSignOut}
          disabled={signingOut}
          className="fm-btn fm-btn-quiet"
        >
          {signingOut ? t("Signing out…") : t("Sign out")}
        </button>
        {logoutError && <p role="alert" className="fm-error" style={{ width: "100%" }}>{logoutError}</p>}
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
    </div>
  );
}
