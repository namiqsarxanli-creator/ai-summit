"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiPost, apiRequest, type ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Panel = { id: number; event_id: number; code: string; title: string; starts_at: string; ends_at: string; qr_version: number };
type ScopeRow = { id: number; staff_account_id: number; scope: string; target_id: number | null; revoked: boolean };

const input = "min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-base text-(--fm-ink)";

function Field({ label, value, set }: { label: string; value: string; set: (v: string) => void }) {
  const { t } = useLanguage();
  return (
    <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">
      {t(label)}
      <input className={input} value={value} onChange={(e) => set(e.target.value)} />
    </label>
  );
}

const int = (v: string) => (v.trim() === "" ? null : Number(v.trim()));

// The Beyond Now competition in the admin panel: activation, the close, panels
// (and their screen codes), volunteer scopes and who competes. Every change is
// audited by the server.
export function CompetitionAdmin() {
  const { t } = useLanguage();
  const [panels, setPanels] = useState<Panel[]>([]);
  const [scopes, setScopes] = useState<ScopeRow[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [eventId, setEventId] = useState("");
  const [closesAt, setClosesAt] = useState("");
  const [topN, setTopN] = useState("");
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [starts, setStarts] = useState("");
  const [ends, setEnds] = useState("");
  const [panelId, setPanelId] = useState("");
  const [staffId, setStaffId] = useState("");
  const [scope, setScope] = useState("MASTERCLASS_DOOR");
  const [target, setTarget] = useState("");
  const [scopeId, setScopeId] = useState("");
  const [userId, setUserId] = useState("");
  const [excluded, setExcluded] = useState("");

  const apply = useCallback((p: ApiResult<Panel[]>, s: ApiResult<ScopeRow[]>) => {
    if (p.ok) setPanels(p.data);
    if (s.ok) setScopes(s.data);
    const failed = !p.ok ? p : !s.ok ? s : null;
    if (failed && !failed.ok) setError(messageFor(failed.error.code));
  }, []);
  const load = useCallback(
    () => Promise.all([apiGet<Panel[]>("/api/admin/panels"), apiGet<ScopeRow[]>("/api/admin/staff-scopes")]).then(([p, s]) => apply(p, s)),
    [apply],
  );
  useEffect(() => {
    let active = true;
    void Promise.all([apiGet<Panel[]>("/api/admin/panels"), apiGet<ScopeRow[]>("/api/admin/staff-scopes")]).then(([p, s]) => {
      if (active) apply(p, s);
    });
    return () => { active = false; };
  }, [apply]);

  async function change(run: () => Promise<ApiResult<unknown>>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setNotice(""); setError("");
    try {
      const r = await run();
      if (r.ok) { setNotice("Change saved and recorded in the admin audit."); await load(); } else setError(messageFor(r.error.code));
    } finally {
      inFlight.current = false; setBusy(false);
    }
  }

  return (
    <div className="grid gap-5">
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Activation")}</legend>
        <p className="text-sm text-(--fm-ink-soft)">{t("Run the read-only report first: python -m app.cli competition-report. Turning this on ranks only the competition; old booth points stay as history.")}</p>
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => { if (window.confirm(t("Confirm this change?"))) void change(() => apiRequest("/api/admin/event-config", "PATCH", { competition_mode: true })); }}>{t("Turn the competition ranking on")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest("/api/admin/event-config", "PATCH", { competition_mode: false }))}>{t("Turn it off (legacy ranking)")}</button>
        <p className="text-sm text-(--fm-ink-soft)">{t("Reminder emails go only to attendees who opted in, through the sign-in email provider. Leave them off until the release is approved.")}</p>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => { if (window.confirm(t("Confirm this change?"))) void change(() => apiRequest("/api/admin/event-config", "PATCH", { reminders_enabled: true })); }}>{t("Turn reminder emails on")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest("/api/admin/event-config", "PATCH", { reminders_enabled: false }))}>{t("Turn reminder emails off")}</button>
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Competition closes at (with offset, e.g. 2026-11-13T17:30+04:00)" value={closesAt} set={setClosesAt} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest(`/api/admin/masterclass-events/${int(eventId)}`, "PATCH", { competition_closes_at: closesAt.trim() || null }))}>{t("Save the close")}</button>
        <Field label="Leaderboard top N" value={topN} set={setTopN} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiRequest("/api/admin/event-config", "PATCH", { leaderboard_top_n: int(topN) }))}>{t("Set top N")}</button>
      </fieldset>

      <section className="grid gap-2">
        <h3 className="font-semibold">{t("Main Stage panels")}</h3>
        {panels.length === 0 ? <p className="text-sm text-(--fm-muted)">{t("Nobody yet.")}</p> : (
          <ul className="grid gap-1 text-sm">
            {panels.map((p) => (
              <li key={p.id}>#{p.id} {p.title} · v{p.qr_version} · <Link className="text-(--fm-red) underline" href={`/screen/panel?panel=${p.id}`}>{t("Screen codes")}</Link></li>
            ))}
          </ul>
        )}
      </section>
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Add a panel")}</legend>
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Code" value={code} set={setCode} />
        <Field label="Title" value={title} set={setTitle} />
        <Field label="Starts (with offset, e.g. 2026-11-13T11:30+04:00)" value={starts} set={setStarts} />
        <Field label="Ends" value={ends} set={setEnds} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiPost("/api/admin/panels", { event_id: int(eventId), code: code.trim(), title: title.trim(), starts_at: starts.trim(), ends_at: ends.trim() }))}>{t("Save panel")}</button>
        <Field label="Panel ID" value={panelId} set={setPanelId} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => { if (window.confirm(t("Every code shown before stops working. Continue?"))) void change(() => apiRequest(`/api/admin/panels/${int(panelId)}`, "PATCH", { rotate_qr: true })); }}>{t("Rotate the screen codes")}</button>
      </fieldset>

      <section className="grid gap-2">
        <h3 className="font-semibold">{t("Staff scopes")}</h3>
        <ul className="grid gap-1 text-sm">
          {scopes.map((s) => <li key={s.id}>#{s.id} · staff {s.staff_account_id} · {s.scope}{s.target_id ? ` · ${s.target_id}` : ""}{s.revoked ? ` · ${t("revoked")}` : ""}</li>)}
        </ul>
      </section>
      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Grant or revoke a scope")}</legend>
        <Field label="Staff ID" value={staffId} set={setStaffId} />
        <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Scope")}
          <select className={input} value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="MASTERCLASS_DOOR">{t("Masterclass door")}</option>
            <option value="GAME_MASTER">{t("Game Master (Qmeter)")}</option>
            <option value="MENTOR_HOST">{t("Mentor group host")}</option>
          </select>
        </label>
        <Field label="Target ID (masterclass or mentor group; empty for Game Master)" value={target} set={setTarget} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiPost("/api/admin/staff-scopes", { staff_account_id: int(staffId), scope, target_id: int(target) }))}>{t("Grant scope")}</button>
        <Field label="Scope ID to revoke" value={scopeId} set={setScopeId} />
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(() => apiPost(`/api/admin/staff-scopes/${int(scopeId)}/revoke`, {}))}>{t("Revoke scope")}</button>
      </fieldset>

      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) p-5">
        <legend className="px-1 text-lg font-medium">{t("Who competes")}</legend>
        <p className="text-sm text-(--fm-ink-soft)">{t("The organiser team, volunteers, sponsors and speakers do not compete. Only an administrator can change this.")}</p>
        <Field label="Attendee ID" value={userId} set={setUserId} />
        <Field label="Reason (empty = takes part)" value={excluded} set={setExcluded} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiRequest(`/api/admin/users/${int(userId)}/competition-eligibility`, "PATCH", { excluded_reason: excluded.trim() || null }))}>{t("Save")}</button>
      </fieldset>

      {notice && <p role="status" className="rounded-xl border border-(--fm-ok-line) bg-(--fm-ok-soft) p-3 text-sm text-(--fm-ok)">{t(notice)}</p>}
      {error && <p role="alert" className="text-(--fm-warn)">{t(error)}</p>}
    </div>
  );
}
