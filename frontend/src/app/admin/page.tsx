"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/ui/LanguageProvider";

import { apiGet, apiPost, apiRequest } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { startStaffPasswordReset } from "@/lib/staffPassword";
import { AdminList } from "@/components/admin/AdminList";
import { AuditLog } from "@/components/admin/AuditLog";
import { PlaygroundAdmin } from "@/components/admin/PlaygroundAdmin";
import { CompetitionAdmin } from "@/components/admin/CompetitionAdmin";
import { ConnectionHubAdmin } from "@/components/admin/ConnectionHubAdmin";
import { MasterclassAdmin } from "@/components/admin/MasterclassAdmin";
import { Records, type RecordValue } from "@/components/admin/Records";

type Section = "competition" | "connection-hub" | "playground" | "attendees" | "masterclasses" | "booths" | "interests" | "staff" | "tasks" | "presets" | "event-config" | "points-log" | "audit" | "otp-deliveries" | "demo-otps";

const sections: Section[] = ["attendees", "competition", "connection-hub", "playground", "masterclasses", "booths", "interests", "staff", "tasks", "presets", "event-config", "points-log", "audit", "otp-deliveries", "demo-otps"];
const titles: Record<Section, string> = { competition: "Beyond Now competition", "connection-hub": "Connection Hub", playground: "AI Playground", attendees: "Attendees", masterclasses: "Masterclass programme", booths: "Booths", interests: "Interests", staff: "Staff", tasks: "Tasks", presets: "Point presets (legacy)", "event-config": "Event settings", "points-log": "Points ledger", audit: "Audit log", "otp-deliveries": "Email delivery", "demo-otps": "Demo codes" };

type DeliveryPage = {
  items: RecordValue[];
  next_before_id: number | null;
  summary: { counts: Record<string, number>; due_now: number };
  note: string;
};

// OTP email status: newest first, older pages on demand. The server never
// sends the code or the full address, so there is nothing to hide here.
function Deliveries({ page }: { page: DeliveryPage }) {
  const { t } = useLanguage();
  const [older, setOlder] = useState<RecordValue[]>([]);
  const [cursor, setCursor] = useState<number | null | undefined>(undefined);
  const [failure, setFailure] = useState("");
  const next = cursor === undefined ? page.next_before_id : cursor;
  async function loadOlder() {
    try {
      const result = (await request(`otp-deliveries?before_id=${next}`)) as DeliveryPage;
      setOlder((current) => [...current, ...result.items]);
      setCursor(result.next_before_id);
      setFailure("");
    } catch (cause) { setFailure(cause instanceof Error ? cause.message : "Request failed"); }
  }
  return <div className="grid gap-3">
    <p className="text-sm text-(--fm-ink-soft)">{t(page.note)}</p>
    <p className="text-sm text-(--fm-ink)">{Object.entries(page.summary.counts).map(([status, total]) => `${t(status)}: ${total}`).join(" · ")}</p>
    {page.summary.due_now > 0 && <p className="text-sm text-(--fm-warn)">{t("Waiting to be sent now")}: {page.summary.due_now}</p>}
    <Records data={[...page.items, ...older]} />
    {next !== null && <button className="min-h-11 w-fit rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2" onClick={() => void loadOlder()}>{t("Load older")}</button>}
    {failure && <p role="alert" className="text-(--fm-warn)">{t(failure)}</p>}
  </div>;
}

async function request(path: string, method = "GET", body?: RecordValue): Promise<unknown> {
  const endpoint = `/api/admin/${path}`;
  const result = method === "GET"
    ? await apiGet<unknown>(endpoint)
    : method === "POST"
      ? await apiPost<unknown>(endpoint, body)
      : await apiRequest<unknown>(endpoint, method as "PATCH" | "PUT" | "DELETE", body);
  if (!result.ok) throw new Error(messageFor(result.error.code));
  return result.data;
}

function Field({ label, value, set }: { label: string; value: string; set: (value: string) => void }) {
  const { t } = useLanguage();
  return <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t(label)}
    <input className="min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-base text-(--fm-ink)" value={value} onChange={(event) => set(event.target.value)} />
  </label>;
}

export default function AdminPage() {
  const { t, language } = useLanguage();
  const [section, setSection] = useState<Section>("attendees");
  // What a plain section loaded, tagged with the section it belongs to, so
  // records of one section can never be shown under another's heading.
  const [loaded, setLoaded] = useState<{ section: Section; value: unknown } | null>(null);
  const data = loaded !== null && loaded.section === section ? loaded.value : null;
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const changeInFlight = useRef(false);
  const [loading, setLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [signedIn, setSignedIn] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [boothId, setBoothId] = useState("");
  const [interestId, setInterestId] = useState("");
  const [recordId, setRecordId] = useState("");
  // The reset email: which staff member, and the language of the letter.
  const [resetId, setResetId] = useState("");
  const [resetLanguage, setResetLanguage] = useState("");
  const [points, setPoints] = useState("");
  const [weight, setWeight] = useState("");
  // The "Demo codes" tab exists only on a DEMO_MODE backend: the endpoint
  // answers with an error everywhere else, so probe it once after sign-in
  // instead of offering a tab that can only fail.
  const [demoOtpsAvailable, setDemoOtpsAvailable] = useState(false);
  useEffect(() => {
    if (!signedIn) return;
    let cancelled = false;
    void apiGet<unknown>("/api/admin/demo-otps").then((result) => {
      if (!cancelled) setDemoOtpsAvailable(result.ok);
    });
    return () => { cancelled = true; };
  }, [signedIn]);
  const [role, setRole] = useState("BOOTH_STAFF");
  const [repeatDefault, setRepeatDefault] = useState(false);
  // An activity's repeat policy: inherit the booth / event setting, or override it.
  const [taskRepeat, setTaskRepeat] = useState<"inherit" | "allow" | "deny">("inherit");
  const taskRepeatValue = taskRepeat === "inherit" ? null : taskRepeat === "allow";

  // Every load of a plain section takes a number; only the newest number may
  // write to the screen. Switching section, pressing Refresh and the refresh
  // that follows a change all start a load, so an answer that arrives after a
  // newer load began (or after the section changed) is dropped, never shown
  // under the wrong heading.
  const newestLoad = useRef(0);
  const sectionNow = useRef<Section>(section);
  useEffect(() => { sectionNow.current = section; }, [section]);
  // The paged lists (attendees, ledger) own their loading; this makes them reload.
  const [refreshKey, setRefreshKey] = useState(0);
  // Sections whose component loads (and reloads) its own data.
  const paged = (item: Section) => item === "competition" || item === "connection-hub" || item === "playground" || item === "attendees" || item === "points-log" || item === "audit" || item === "masterclasses";

  const load = useCallback((target: Section): Promise<void> => {
    const mine = ++newestLoad.current;
    return request(target).then((value) => {
      if (mine !== newestLoad.current) return;
      setLoaded({ section: target, value }); setError("");
    }, (cause: unknown) => {
      if (mine !== newestLoad.current) return;
      setError(cause instanceof Error ? cause.message : "Request failed");
    }).finally(() => {
      if (mine === newestLoad.current) setLoading(false);
    });
  }, []);

  // Reloads whichever section is on screen NOW (not the one a click started
  // from): a change saved just before the user switched tabs must not paint
  // its old section over the new one.
  const refresh = useCallback(async () => {
    const target = sectionNow.current;
    if (paged(target)) { setRefreshKey((key) => key + 1); return; }
    setLoading(true);
    await load(target);
  }, [load]);

  useEffect(() => {
    if (!signedIn) return;
    newestLoad.current += 1; // anything still in flight belongs to the section we are leaving
    if (paged(section)) return; // the paged lists load themselves
    void load(section);
  }, [section, signedIn, load]);

  async function login() {
    try {
      const signed = await apiPost<null>("/api/staff/auth/login", { email, password });
      if (!signed.ok) throw new Error("Sign in failed");
      const me = await apiGet<{ role?: string }>("/api/staff/me");
      if (!me.ok || me.data.role !== "ADMIN") throw new Error("This account is not an administrator");
      setSignedIn(true); setPassword(""); setError("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Sign in failed"); }
  }

  async function change(path: string, method: string, body?: RecordValue) {
    if (changeInFlight.current) return;
    if ((path.endsWith("/deactivate") || method === "DELETE") && !window.confirm(t("Confirm this change?"))) return;
    changeInFlight.current = true;
    setBusy(true); setNotice("");
    try { await request(path, method, body); await refresh(); setError(""); setNotice("Change saved and recorded in the admin audit."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Change failed"); }
    finally { changeInFlight.current = false; setBusy(false); }
  }

  // An administrator makes the system email a one-time code to the staff member's
  // OWN address. The page never sees the code and cannot choose the new password.
  async function sendResetEmail() {
    if (changeInFlight.current) return;
    if (!/^[1-9][0-9]{0,15}$/.test(resetId.trim())) { setError("Enter a whole number greater than zero."); return; }
    if (!window.confirm(t("Send a password reset email to this staff member? They get a one-time code at their own address and choose the new password themselves; you will not see either."))) return;
    changeInFlight.current = true;
    setBusy(true); setNotice(""); setError("");
    const result = await startStaffPasswordReset(Number(resetId.trim()), resetLanguage || language);
    changeInFlight.current = false;
    setBusy(false);
    if (result.ok) setNotice("A reset email was queued for that staff member. You cannot see the code, and only they can choose the new password.");
    else setError(messageFor(result.error.code));
  }

  const id = Number(recordId);
  const booth = Number(boothId);
  const interest = Number(interestId);
  const pointValue = Number(points);
  const weightValue = Number(weight);

  return <main id="main-content" className="fm-admin mx-auto flex max-w-5xl flex-col gap-6 px-4 py-8 text-(--fm-ink) sm:px-6">
    <header><p className="text-xs font-bold uppercase tracking-[0.25em] text-(--fm-red)">{t("AI Summit · Operations")}</p><h1 className="mt-2 text-3xl font-bold tracking-tight">{t("AI Summit admin")}</h1><p className="mt-2 text-sm text-(--fm-ink-soft)">{t("Manage event setup and inspect activity.")}</p></header>
    {!signedIn ? <section className="grid max-w-sm gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 shadow-[0_8px_21px_#42151a10]">
      <Field label={t("Staff email" )} value={email} set={setEmail} />
      <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Password")}
        <input type="password" autoComplete="current-password" className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-white p-3 text-base text-(--fm-ink)" value={password} onChange={(event) => setPassword(event.target.value)} />
      </label>
      <button className="min-h-11 rounded-xl bg-(--fm-red) text-white p-3 font-semibold" onClick={() => void login()}>{t("Sign in")}</button>
    </section> : <>
      <nav className="-mx-4 flex snap-x gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0" aria-label={t("Admin sections")}>
        {sections.filter((item) => item !== "demo-otps" || demoOtpsAvailable).map((item) => <button key={item} aria-current={item === section ? "page" : undefined} className={`min-h-11 shrink-0 snap-start whitespace-nowrap rounded-xl px-3 py-2 text-sm font-semibold ${item === section ? "bg-(--fm-red) text-white text-(--fm-ink)" : "border border-(--fm-line-strong) bg-(--fm-tint) text-(--fm-ink)"}`} onClick={() => { setSection(item); setNotice(""); setError(""); }}>{t(titles[item])}</button>)}
      </nav>
      <div className="flex flex-wrap gap-2">
        <button className="min-h-11 w-fit rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2" onClick={() => void refresh()}>{t("Refresh")}</button>
        <Link href="/staff/password" className="inline-flex min-h-11 items-center rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2">{t("Change my password")}</Link>
      </div>
      <section className="min-w-0 rounded-2xl border border-(--fm-line) bg-white p-4" aria-label={`${section} data`}>
        <h2 className="mb-4 text-xl font-semibold">{t(titles[section])}</h2>
        {section === "competition" ? <CompetitionAdmin key={`${section}-${refreshKey}`} /> : section === "connection-hub" ? <ConnectionHubAdmin key={`${section}-${refreshKey}`} /> : section === "playground" ? <PlaygroundAdmin /> : section === "masterclasses"
          ? <MasterclassAdmin key={`${section}-${refreshKey}`} />
          : section === "audit"
          ? <AuditLog key={section} refreshKey={refreshKey} />
          : paged(section)
          ? <AdminList key={section} kind={section as "attendees" | "points-log"} refreshKey={refreshKey} />
          : <>
            {(loading || (data === null && !error)) && <p role="status" className="text-sm text-(--fm-ink-soft)">{t("Loading records…")}</p>}
            {!loading && data !== null && (section === "otp-deliveries" && Array.isArray((data as DeliveryPage).items)
              ? <Deliveries key={`${String((data as DeliveryPage).items[0]?.id)}-${JSON.stringify((data as DeliveryPage).summary)}`} page={data as DeliveryPage} />
              : <Records data={data} />)}
          </>}
      </section>
      {section === "attendees" && <p className="text-sm text-(--fm-ink-soft)">{t("Attendee records are for inspection only.")}</p>}
      {section === "points-log" && <p className="text-sm text-(--fm-ink-soft)">{t("The ledger itself is never edited. An award can be reversed in full, with a reason: the reversal is a new entry linked to it.")}</p>}
      {section === "demo-otps" && <p className="text-sm text-(--fm-ink-soft)">{t("Recent codes are visible only in demo mode to signed-in administrators.")}</p>}
      {section !== "attendees" && section !== "competition" && section !== "masterclasses" && section !== "playground" && section !== "points-log" && section !== "audit" && section !== "otp-deliveries" && section !== "demo-otps" && <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 disabled:opacity-60">
        <h2 className="text-lg font-medium">{t("Manage")} {t(titles[section])}</h2>
        {section === "staff" && <>
          <Field label={t("Email" )} value={email} set={setEmail} /><Field label={t("Full name" )} value={name} set={setName} />
          <Field label={t("New password" )} value={password} set={setPassword} />
          <label>Role <select className="bg-white p-2" value={role} onChange={(event) => setRole(event.target.value)}><option>BOOTH_STAFF</option><option>ADMIN</option></select></label>
          <Field label={t("Booth ID (booth staff only)" )} value={boothId} set={setBoothId} />
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("staff", "POST", { email, password, full_name: name, role, booth_id: role === "ADMIN" ? null : booth })}>{t("Create staff")}</button>
          <Field label={t("Staff ID to deactivate" )} value={recordId} set={setRecordId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`staff/${id}/deactivate`, "POST")}>{t("Deactivate staff")}</button>
          <Field label={t("Staff ID for the reset email" )} value={resetId} set={setResetId} />
          <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Email language")}
            <select className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-white p-3 text-(--fm-ink)" value={resetLanguage || language} onChange={(event) => setResetLanguage(event.target.value)}>
              <option value="az">Azərbaycan</option><option value="en">English</option><option value="ru">Русский</option>
            </select>
          </label>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void sendResetEmail()}>{t("Send password reset email")}</button>
        </>}
        {section === "booths" && <>
          <Field label={t("Slug" )} value={slug} set={setSlug} /><Field label={t("Name" )} value={name} set={setName} />
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("booths", "POST", { slug, name })}>{t("Create booth")}</button>
          <Field label={t("Booth ID" )} value={boothId} set={setBoothId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`booths/${booth}`, "PATCH", { name })}>{t("Rename booth")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`booths/${booth}`, "PATCH", { is_active: false })}>{t("Deactivate booth")}</button>
          <Field label={t("Interest ID" )} value={interestId} set={setInterestId} /><Field label={t("Weight (0.01–1.00)" )} value={weight} set={setWeight} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`booths/${booth}/weights/${interest}`, "PUT", { weight: weightValue })}>{t("Set interest weight")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`booths/${booth}/weights/${interest}`, "DELETE")}>{t("Remove interest weight")}</button>
        </>}
        {section === "interests" && <>
          <Field label={t("Slug" )} value={slug} set={setSlug} /><Field label={t("Name" )} value={name} set={setName} />
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("interests", "POST", { slug, name })}>{t("Create interest")}</button>
          <Field label={t("Interest ID" )} value={interestId} set={setInterestId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`interests/${interest}`, "PATCH", { name })}>{t("Rename interest")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`interests/${interest}`, "PATCH", { is_active: false })}>{t("Deactivate interest")}</button>
        </>}
        {section === "tasks" && <>
          <Field label={t("Booth ID" )} value={boothId} set={setBoothId} /><Field label={t("Title" )} value={name} set={setName} />
          <Field label={t("Points per award (1–100)" )} value={points} set={setPoints} />
          <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Repeat policy")}
            <select className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-white p-3 text-(--fm-ink)" value={taskRepeat} onChange={(event) => setTaskRepeat(event.target.value as "inherit" | "allow" | "deny")}>
              <option value="inherit">{t("Inherit from booth / event")}</option>
              <option value="allow">{t("Allow repeats")}</option>
              <option value="deny">{t("Once per attendee")}</option>
            </select>
          </label>
          <p className="text-xs text-(--fm-muted)">{t("Staff cannot change the points: each award pays exactly this value.")}</p>
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("tasks", "POST", { booth_id: booth, title: name, points: pointValue, allow_repeat_points: taskRepeatValue })}>{t("Create task")}</button>
          <Field label={t("Task ID" )} value={recordId} set={setRecordId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { points: pointValue })}>{t("Set task points")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { allow_repeat_points: taskRepeatValue })}>{t("Set repeat policy")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { title: name })}>{t("Rename task")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { is_active: false })}>{t("Deactivate task")}</button>
        </>}
        {section === "presets" && <>
          <div role="note" className="grid gap-1 rounded border border-(--fm-warn-line) bg-(--fm-warn-soft) p-3 text-sm text-(--fm-warn)">
            <p className="font-semibold">{t("Legacy — these presets no longer affect new activity points.")}</p>
            <p>{t("Awards now pay the points set on each activity (Tasks). Presets are kept as-is for history and are not used when awarding.")}</p>
          </div>
          <Field label={t("Booth ID (blank for global)" )} value={boothId} set={setBoothId} /><Field label={t("Points" )} value={points} set={setPoints} />
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("presets", "POST", { booth_id: boothId === "" ? null : booth, points: pointValue })}>{t("Create preset")}</button>
          <Field label={t("Preset ID" )} value={recordId} set={setRecordId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`presets/${id}`, "PATCH", { points: pointValue })}>{t("Change preset value")}</button>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change(`presets/${id}`, "PATCH", { is_active: false })}>{t("Deactivate preset")}</button>
        </>}
        {section === "event-config" && <>
          <Field label={t("Feedback reward points" )} value={points} set={setPoints} />
          <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change("event-config", "PATCH", { feedback_points: pointValue })}>{t("Set feedback points")}</button>
          <Field label={t("Leaderboard top N" )} value={recordId} set={setRecordId} />
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change("event-config", "PATCH", { leaderboard_top_n: id })}>{t("Set top N")}</button>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={repeatDefault} onChange={(event) => setRepeatDefault(event.target.checked)} />{t("Allow repeat task points by default")}</label>
          <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => void change("event-config", "PATCH", { allow_repeat_task_points_default: repeatDefault })}>{t("Set repeat default")}</button>
        </>}
      </fieldset>}
    </>}
    {notice && <p role="status" className="rounded-xl border border-(--fm-ok-line) bg-(--fm-ok-soft) p-3 text-sm text-(--fm-ok)">{t(notice)}</p>}
    {error && <p role="alert" className="text-(--fm-warn)">{t(error)}</p>}
  </main>;
}
