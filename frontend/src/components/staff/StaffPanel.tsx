"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { fetchPresets, fetchStaffMe, staffLogout, type Preset, type StaffMe } from "@/lib/staff";

type PresetsState = { kind: "none" } | { kind: "ok"; presets: Preset[] } | { kind: "error"; code: string };

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; me: StaffMe; presets: PresetsState }
  | { kind: "error"; code: string };

function isSignedOut(code: string): boolean {
  // §20.3: UNAUTHENTICATED (no/expired/revoked session) and
  // STAFF_NOT_AUTHORIZED (an attendee session, or an inactive account)
  // both just mean "this screen has no one to show" from here.
  return code === "UNAUTHENTICATED" || code === "STAFF_NOT_AUTHORIZED";
}

// M09's staff panel: identity and booth binding come ONLY from the
// server-resolved session (§12.2, §24.3) — nothing here lets the staff
// member pick a different booth. Presets are shown read-only, proving
// the allow-list is real and server-derived; awarding itself is M10/M11.
export function StaffPanel() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchStaffMe().then(async (meResult) => {
      if (cancelled) return;
      if (!meResult.ok) {
        setState(
          isSignedOut(meResult.error.code)
            ? { kind: "signed-out" }
            : { kind: "error", code: meResult.error.code },
        );
        return;
      }
      // Presets only exist for booth staff (§12.3: admins cannot
      // award) — an admin session simply shows none, not an error.
      if (meResult.data.role !== "BOOTH_STAFF") {
        setState({ kind: "ok", me: meResult.data, presets: { kind: "none" } });
        return;
      }
      const presetsResult = await fetchPresets();
      if (cancelled) return;
      setState({
        kind: "ok",
        me: meResult.data,
        presets: presetsResult.ok
          ? { kind: "ok", presets: presetsResult.data }
          : { kind: "error", code: presetsResult.error.code },
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSignOut() {
    await staffLogout();
    setState({ kind: "signed-out" });
  }

  if (state.kind === "loading") {
    return <p className="text-sm text-slate-500">Checking your session…</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-slate-400">Sign in with your staff account to continue.</p>
        <Link
          href="/staff/login"
          className="w-full rounded-lg bg-indigo-500 px-4 py-3 text-center font-semibold text-white transition-colors hover:bg-indigo-400"
        >
          Staff sign in
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

  const { me, presets } = state;
  return (
    <div className="flex flex-col gap-6">
      <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-emerald-400">
          Signed in
        </p>
        <p className="mt-1 text-lg font-semibold text-slate-50">{me.full_name}</p>
        <p className="text-sm text-slate-400">
          {me.role === "ADMIN" ? "Admin" : (me.booth_name ?? "No booth assigned")}
        </p>
      </section>

      {me.role === "BOOTH_STAFF" && (
        <section className="flex flex-col gap-3">
          <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
            Point values for {me.booth_name}
          </h2>
          {presets.kind === "error" && (
            <p role="alert" className="text-sm text-amber-300">
              {messageFor(presets.code)}
            </p>
          )}
          {presets.kind === "ok" && presets.presets.length === 0 && (
            <p className="text-sm text-slate-500">No point presets are configured yet.</p>
          )}
          {presets.kind === "ok" && presets.presets.length > 0 && (
            <ul aria-label="Point presets" className="flex flex-wrap gap-2">
              {presets.presets.map((preset) => (
                <li
                  key={preset.points}
                  className="rounded-full border border-indigo-500/30 bg-indigo-500/10 px-4 py-2 text-sm font-semibold text-indigo-200"
                >
                  +{preset.points}
                  {preset.label ? ` · ${preset.label}` : ""}
                </li>
              ))}
            </ul>
          )}
          <p className="text-xs text-slate-500">
            Scanning and awarding points isn&apos;t available yet — check back soon.
          </p>
        </section>
      )}

      <button
        type="button"
        onClick={onSignOut}
        className="rounded-lg border border-slate-700 px-4 py-2.5 text-sm font-semibold text-slate-200 transition-colors hover:bg-slate-800"
      >
        Sign out
      </button>
    </div>
  );
}
