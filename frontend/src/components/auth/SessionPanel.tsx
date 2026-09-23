"use client";

import Link from "next/link";
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
      setLogoutError(messageFor(result.error.code));
    }
    setSigningOut(false);
  }

  if (state.kind === "loading") {
    return <p className="text-sm text-slate-500">Checking your session…</p>;
  }

  if (state.kind === "signed-in") {
    const { attendee } = state;
    return (
      <div className="flex w-full flex-col gap-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-5 text-left">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-400">
            Signed in
          </p>
          <p className="mt-1 text-lg font-semibold text-slate-50">
            {attendee.first_name} {attendee.last_name}
          </p>
          <p className="text-sm text-slate-400">{attendee.profession}</p>
          <p className="text-sm text-slate-500">{attendee.email}</p>
        </div>
        {logoutError && (
          <p role="alert" className="text-sm text-red-300">
            {logoutError}
          </p>
        )}
        <button
          type="button"
          onClick={onSignOut}
          disabled={signingOut}
          className="rounded-lg border border-slate-700 px-4 py-2.5 text-sm font-semibold text-slate-200 transition-colors hover:bg-slate-800 disabled:opacity-60"
        >
          {signingOut ? "Signing out…" : "Sign out"}
        </button>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-3">
      {state.kind === "error" && (
        <p role="alert" className="text-sm text-amber-300">
          {messageFor(state.error.code)}
        </p>
      )}
      <Link
        href="/register"
        className="w-full rounded-lg bg-indigo-500 px-4 py-3 font-semibold text-white transition-colors hover:bg-indigo-400"
      >
        Register as an attendee
      </Link>
      <Link
        href="/login"
        className="w-full rounded-lg border border-slate-700 px-4 py-3 font-semibold text-slate-200 transition-colors hover:bg-slate-800"
      >
        Already registered? Sign in
      </Link>
    </div>
  );
}
