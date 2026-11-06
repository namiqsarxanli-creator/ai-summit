"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";
import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { competitionRankingOn } from "@/lib/final";
import { fetchStaffMe, fetchStaffTasks, staffLogout, type StaffMe, type StaffTask } from "@/lib/staff";

type TasksState = { kind: "none" } | { kind: "ok"; tasks: StaffTask[] } | { kind: "error"; code: string };

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; me: StaffMe; tasks: TasksState }
  | { kind: "error"; code: string };

function isSignedOut(code: string): boolean {
  // §20.3: UNAUTHENTICATED (no/expired/revoked session) and
  // STAFF_NOT_AUTHORIZED (an attendee session, or an inactive account)
  // both just mean "this screen has no one to show" from here.
  return code === "UNAUTHENTICATED" || code === "STAFF_NOT_AUTHORIZED";
}

// M09's staff panel: identity and booth binding come ONLY from the
// server-resolved session (§12.2, §24.3) — nothing here lets the staff
// member pick a different booth. The booth's activities are shown read-only
// with the points each is worth (server-derived); awarding happens in the
// scanner, after a scan.
export function StaffPanel() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  // The old booth-activity points do not count in the Beyond Now ranking, so
  // its scanner is not offered while that ranking is on (unknown: offered).
  const [legacyScan, setLegacyScan] = useState(true);
  useEffect(() => {
    let active = true;
    void competitionRankingOn().then((on) => { if (active && on !== null) setLegacyScan(!on); });
    return () => { active = false; };
  }, []);

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
      // Activities only exist for booth staff (§12.3: admins cannot
      // award) — an admin session simply shows none, not an error.
      if (meResult.data.role !== "BOOTH_STAFF") {
        setState({ kind: "ok", me: meResult.data, tasks: { kind: "none" } });
        return;
      }
      const tasksResult = await fetchStaffTasks();
      if (cancelled) return;
      setState({
        kind: "ok",
        me: meResult.data,
        tasks: tasksResult.ok
          ? { kind: "ok", tasks: tasksResult.data }
          : { kind: "error", code: tasksResult.error.code },
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
    return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  }

  if (state.kind === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in with your staff account to continue.")}</p>
        <Link href="/staff/login" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Staff sign in")}
        </Link>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { me, tasks } = state;
  return (
    <div className="fm-stack-lg">
      <section className="fm-staff-identity fm-stack" style={{ gap: 4 }}>
        <p className="fm-eyebrow">{t("Signed in")}</p>
        <p className="fm-h2">{me.full_name}</p>
        <p className="fm-small">
          {me.role === "ADMIN" ? t("Admin") : me.role === "VOLUNTEER" ? t("Volunteer") : (me.booth_name ? t(me.booth_name) : t("No booth assigned"))}
        </p>
        {me.staff_id != null && (
          <p className="fm-micro">
            {t("Staff ID")}: {me.staff_id}{me.booth_id != null && <> · {t("Booth ID")}: {me.booth_id}</>}
          </p>
        )}
      </section>

      <Link href="/staff/attendance" className="fm-btn fm-btn-primary fm-btn-block">{t("Attendance scanning")}</Link>
      {/* The server decides who may review: a Game Master scope or an administrator. */}
      {me.role !== "BOOTH_STAFF" && <Link href="/staff/final" className="fm-btn fm-btn-secondary fm-btn-block">{t("Final result")}</Link>}
      {me.role === "BOOTH_STAFF" && <Link href="/staff/booth-report" className="fm-btn fm-btn-secondary fm-btn-block">{t("Booth report")}</Link>}
      <Link href="/staff/playground" className="fm-btn fm-btn-secondary fm-btn-block">{t("Playground arrivals and queue")}</Link>
      {me.role !== "VOLUNTEER" && legacyScan && (
        <Link href="/staff/scan" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Scan attendee")}
        </Link>
      )}

      <Link href="/staff/password" className="fm-btn fm-btn-secondary fm-btn-block">
        {t("Change password")}
      </Link>

      {me.role === "BOOTH_STAFF" && legacyScan && (
        <section className="fm-section">
          <h2 className="fm-eyebrow">
            {t("Activities for")} {me.booth_name && t(me.booth_name)}
          </h2>
          {tasks.kind === "error" && (
            <p role="alert" className="fm-alert fm-alert-warn">
              {t(messageFor(tasks.code))}
            </p>
          )}
          {tasks.kind === "ok" && tasks.tasks.length === 0 && (
            <p className="fm-empty">{t("No activities with a point value are set up for this booth yet.")}</p>
          )}
          {tasks.kind === "ok" && tasks.tasks.length > 0 && (
            <ul aria-label={t("Activities")} className="fm-chips">
              {tasks.tasks.map((task) => (
                <li key={task.id} className="fm-chip">
                  {t(task.title)} · +{task.points}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <button type="button" onClick={onSignOut} className="fm-btn fm-btn-secondary fm-btn-block">
        {t("Sign out")}
      </button>
    </div>
  );
}
