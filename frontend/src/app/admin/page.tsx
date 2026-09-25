"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { apiGet, apiPost, apiRequest } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type RecordValue = Record<string, unknown>;
type Section = "attendees" | "booths" | "interests" | "staff" | "tasks" | "presets" | "event-config" | "points-log" | "demo-otps";

const sections: Section[] = ["attendees", "booths", "interests", "staff", "tasks", "presets", "event-config", "points-log", "demo-otps"];
const titles: Record<Section, string> = { attendees: "Attendees", booths: "Booths", interests: "Interests", staff: "Staff", tasks: "Tasks", presets: "Point presets", "event-config": "Event settings", "points-log": "Points ledger", "demo-otps": "Demo codes" };

function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.map(display).join(", ") : "—";
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key.replaceAll("_", " ")}: ${display(item)}`).join(" · ");
  return String(value);
}

function Records({ data }: { data: unknown }) {
  const rows = Array.isArray(data) ? data as RecordValue[] : data && typeof data === "object" ? [data as RecordValue] : [];
  if (!rows.length) return <p className="rounded-xl border border-slate-700 p-5 text-sm text-slate-300">No records to show yet.</p>;
  return <ul className="grid gap-3" aria-label="Admin records">{rows.map((row, index) =>
    <li key={String(row.id ?? index)} className="min-w-0 rounded-xl border border-slate-700 bg-slate-950/50 p-4">
      <dl className="grid gap-2 sm:grid-cols-2">{Object.entries(row).map(([key, value]) =>
        <div key={key} className="min-w-0"><dt className="text-xs font-bold uppercase tracking-wide text-cyan-300">{key.replaceAll("_", " ")}</dt><dd className="break-words text-sm text-slate-100">{display(value)}</dd></div>
      )}</dl>
    </li>
  )}</ul>;
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
  return <label className="flex flex-col gap-1 text-sm text-slate-300">{label}
    <input className="min-h-11 w-full rounded-xl border border-slate-600 bg-slate-900 p-3 text-white" value={value} onChange={(event) => set(event.target.value)} />
  </label>;
}

export default function AdminPage() {
  const [section, setSection] = useState<Section>("attendees");
  const [data, setData] = useState<unknown>(null);
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
  const [points, setPoints] = useState("");
  const [weight, setWeight] = useState("");
  const [role, setRole] = useState("BOOTH_STAFF");
  const [repeatDefault, setRepeatDefault] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { setData(await request(section)); setError(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Request failed"); }
    finally { setLoading(false); }
  }, [section]);

  useEffect(() => {
    if (!signedIn) return;
    let active = true;
    void request(section).then((value) => {
      if (active) { setData(value); setError(""); }
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : "Request failed");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [section, signedIn]);

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
    if ((path.endsWith("/deactivate") || method === "DELETE") && !window.confirm("Confirm this change?")) return;
    changeInFlight.current = true;
    setBusy(true); setNotice("");
    try { await request(path, method, body); await refresh(); setError(""); setNotice("Change saved and recorded in the admin audit."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Change failed"); }
    finally { changeInFlight.current = false; setBusy(false); }
  }

  const id = Number(recordId);
  const booth = Number(boothId);
  const interest = Number(interestId);
  const pointValue = Number(points);
  const weightValue = Number(weight);

  return <main id="main-content" className="mx-auto flex max-w-5xl flex-col gap-6 px-4 py-8 text-white sm:px-6">
    <header><p className="text-xs font-bold uppercase tracking-[0.25em] text-cyan-300">AI Summit · Operations</p><h1 className="mt-2 text-3xl font-bold tracking-tight">FACEMARK admin</h1><p className="mt-2 text-sm text-slate-300">Manage event setup and inspect activity.</p></header>
    {!signedIn ? <section className="grid max-w-sm gap-3">
      <Field label="Staff email" value={email} set={setEmail} />
      <label className="flex flex-col gap-1 text-sm text-slate-300">Password
        <input type="password" className="min-h-11 rounded-xl border border-slate-600 bg-slate-900 p-3 text-white" value={password} onChange={(event) => setPassword(event.target.value)} />
      </label>
      <button className="min-h-11 rounded-xl bg-indigo-500 p-3 font-semibold" onClick={() => void login()}>Sign in</button>
    </section> : <>
      <nav className="flex flex-wrap gap-2" aria-label="Admin sections">
        {sections.map((item) => <button key={item} aria-current={item === section ? "page" : undefined} className={`min-h-11 rounded-xl px-3 py-2 text-sm font-semibold ${item === section ? "bg-indigo-500 text-white" : "border border-slate-600 bg-slate-800 text-slate-200"}`} onClick={() => { setData(null); setSection(item); setNotice(""); setError(""); }}>{titles[item]}</button>)}
      </nav>
      <button className="min-h-11 w-fit rounded-xl border border-slate-600 bg-slate-800 px-4 py-2" onClick={() => void refresh()}>Refresh</button>
      <section className="min-w-0 rounded-2xl border border-slate-700 bg-slate-900/80 p-4" aria-label={`${section} data`}>
        <h2 className="mb-4 text-xl font-semibold">{titles[section]}</h2>
        {(loading || (data === null && !error)) && <p role="status" className="text-sm text-slate-300">Loading records…</p>}
        {!loading && data !== null && <Records data={data} />}
      </section>
      {section === "attendees" && <p className="text-sm text-slate-300">Attendee records are for inspection only.</p>}
      {section === "points-log" && <p className="text-sm text-slate-300">Read-only ledger audit. No point changes are available here.</p>}
      {section === "demo-otps" && <p className="text-sm text-slate-300">Recent codes are visible only in demo mode to signed-in administrators.</p>}
      {section !== "attendees" && section !== "points-log" && section !== "demo-otps" && <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-slate-700 bg-slate-900/80 p-5 disabled:opacity-60">
        <h2 className="text-lg font-medium">Manage {section.replaceAll("-", " ")}</h2>
        {section === "staff" && <>
          <Field label="Email" value={email} set={setEmail} /><Field label="Full name" value={name} set={setName} />
          <Field label="New password" value={password} set={setPassword} />
          <label>Role <select className="bg-slate-900 p-2" value={role} onChange={(event) => setRole(event.target.value)}><option>BOOTH_STAFF</option><option>ADMIN</option></select></label>
          <Field label="Booth ID (booth staff only)" value={boothId} set={setBoothId} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("staff", "POST", { email, password, full_name: name, role, booth_id: role === "ADMIN" ? null : booth })}>Create staff</button>
          <Field label="Staff ID to deactivate" value={recordId} set={setRecordId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`staff/${id}/deactivate`, "POST")}>Deactivate staff</button>
        </>}
        {section === "booths" && <>
          <Field label="Slug" value={slug} set={setSlug} /><Field label="Name" value={name} set={setName} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("booths", "POST", { slug, name })}>Create booth</button>
          <Field label="Booth ID" value={boothId} set={setBoothId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`booths/${booth}`, "PATCH", { name })}>Rename booth</button>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`booths/${booth}`, "PATCH", { is_active: false })}>Deactivate booth</button>
          <Field label="Interest ID" value={interestId} set={setInterestId} /><Field label="Weight (0.01–1.00)" value={weight} set={setWeight} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`booths/${booth}/weights/${interest}`, "PUT", { weight: weightValue })}>Set interest weight</button>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`booths/${booth}/weights/${interest}`, "DELETE")}>Remove interest weight</button>
        </>}
        {section === "interests" && <>
          <Field label="Slug" value={slug} set={setSlug} /><Field label="Name" value={name} set={setName} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("interests", "POST", { slug, name })}>Create interest</button>
          <Field label="Interest ID" value={interestId} set={setInterestId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`interests/${interest}`, "PATCH", { name })}>Rename interest</button>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`interests/${interest}`, "PATCH", { is_active: false })}>Deactivate interest</button>
        </>}
        {section === "tasks" && <>
          <Field label="Booth ID" value={boothId} set={setBoothId} /><Field label="Title" value={name} set={setName} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("tasks", "POST", { booth_id: booth, title: name })}>Create task</button>
          <Field label="Task ID" value={recordId} set={setRecordId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { title: name })}>Rename task</button>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`tasks/${id}`, "PATCH", { is_active: false })}>Deactivate task</button>
        </>}
        {section === "presets" && <>
          <Field label="Booth ID (blank for global)" value={boothId} set={setBoothId} /><Field label="Points" value={points} set={setPoints} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("presets", "POST", { booth_id: boothId === "" ? null : booth, points: pointValue })}>Create preset</button>
          <Field label="Preset ID" value={recordId} set={setRecordId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`presets/${id}`, "PATCH", { points: pointValue })}>Change preset value</button>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change(`presets/${id}`, "PATCH", { is_active: false })}>Deactivate preset</button>
        </>}
        {section === "event-config" && <>
          <Field label="Feedback reward points" value={points} set={setPoints} />
          <button className="rounded bg-indigo-600 p-2" onClick={() => void change("event-config", "PATCH", { feedback_points: pointValue })}>Set feedback points</button>
          <Field label="Leaderboard top N" value={recordId} set={setRecordId} />
          <button className="rounded bg-slate-700 p-2" onClick={() => void change("event-config", "PATCH", { leaderboard_top_n: id })}>Set top N</button>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={repeatDefault} onChange={(event) => setRepeatDefault(event.target.checked)} />Allow repeat task points by default</label>
          <button className="rounded bg-slate-700 p-2" onClick={() => void change("event-config", "PATCH", { allow_repeat_task_points_default: repeatDefault })}>Set repeat default</button>
        </>}
      </fieldset>}
    </>}
    {notice && <p role="status" className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-200">{notice}</p>}
    {error && <p role="alert" className="text-amber-300">{error}</p>}
  </main>;
}
