"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { fetchProfile, type AttendeeProfile } from "@/lib/profile";
import { logout } from "@/lib/session";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; profile: AttendeeProfile }
  | { kind: "error"; code: string };

function stateFrom(result: ApiResult<AttendeeProfile>): State {
  if (result.ok) return { kind: "ok", profile: result.data };
  const { code } = result.error;
  return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
    ? { kind: "signed-out" }
    : { kind: "error", code };
}

// M08's Profile surface: "registration data, selected interests,
// consent state (private view only)". Every value comes from the
// session-scoped `/api/attendees/me/profile` response — nothing here
// is computed, and nothing here is shown to anyone but the signed-in
// attendee themselves.
export function ProfileView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProfile().then((result) => {
      if (!cancelled) setState(stateFrom(result));
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
      setState(stateFrom(await fetchProfile()));
    } else {
      setLogoutError(messageFor(result.error.code));
    }
    setSigningOut(false);
  }

  if (state.kind === "loading") {
    return <p className="text-sm text-slate-500">Loading your profile…</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-slate-400">Sign in to see your profile.</p>
        <Link
          href="/login"
          className="w-full rounded-lg bg-indigo-500 px-4 py-3 text-center font-semibold text-white transition-colors hover:bg-indigo-400"
        >
          Sign in
        </Link>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="text-sm text-amber-300">
        {messageFor(state.code)}
      </p>
    );
  }

  const { profile } = state;
  return (
    <div className="flex flex-col gap-6">
      <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 className="text-lg font-semibold text-slate-50">
          {profile.first_name} {profile.last_name}
        </h2>
        <p className="text-sm text-slate-400">{profile.profession}</p>
        <dl className="mt-4 flex flex-col gap-2 text-sm">
          <ProfileRow label="Email" value={profile.email} />
          <ProfileRow label="Phone" value={profile.phone} />
          {profile.linkedin_url && <ProfileRow label="LinkedIn" value={profile.linkedin_url} />}
        </dl>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
          Your interests
        </h2>
        {profile.interests.length === 0 ? (
          <p className="text-sm text-slate-500">You haven&apos;t selected any interests yet.</p>
        ) : (
          <ul aria-label="Your interests" className="flex flex-wrap gap-2">
            {profile.interests.map((interest) => (
              <li
                key={interest.slug}
                className="rounded-full border border-indigo-500/30 bg-indigo-500/10 px-3 py-1 text-xs font-medium text-indigo-200"
              >
                {interest.name}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
          Sponsor data sharing
        </h2>
        <p className="text-sm text-slate-300">
          {profile.sponsor_consent
            ? "You agreed to share your details with event sponsors."
            : "You have not agreed to share your details with event sponsors."}
        </p>
        <p className="text-xs text-slate-500">Private — visible only to you.</p>
      </section>

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

      <AttendeeTabBar active="profile" />
    </div>
  );
}

function ProfileRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}
