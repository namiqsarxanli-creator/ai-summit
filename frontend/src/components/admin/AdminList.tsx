"use client";

// Paginated, filterable admin lists (attendees, points ledger).
//
// Paging is server-side keyset paging: the page carries an opaque
// `next_cursor`, and "Previous" is a client-side stack of the cursors already
// used (the server has no "previous" cursor, and does not need one - going back
// re-reads the page from the cursor that started it).
//
// Stale answers can never reach the screen: the loading effect owns a
// `current` flag that its cleanup clears, so any change that starts a newer
// load - a filter, a page move, a refresh after a mutation, leaving the section
// - discards the reply of every older one.
import { useEffect, useState, type FormEvent } from "react";
import { useLanguage } from "@/components/ui/LanguageProvider";

import { apiGet } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { ExportBar } from "./ExportBar";
import { FILTERS, listQuery, problem, sameFilters, type ListKind, type Values } from "./listFilters";
import { Records, type RecordValue } from "./Records";
import { PointsReversal } from "./PointsReversal";
import { canReverse, type LedgerBase, type LedgerEntry } from "@/lib/reversal";

export type { ListKind };

type Page = { items: RecordValue[]; next_cursor: string | null; limit: number };
type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; page: Page };

function errorMessage(code: string): string {
  return code === "VALIDATION_ERROR" ? "The filters are not valid. Check the values and dates, then try again." : messageFor(code);
}

function readable(items: RecordValue[]): RecordValue[] {
  return items.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => {
    if (key.endsWith("_at") && typeof value === "string") {
      const when = new Date(value);
      if (!Number.isNaN(when.getTime())) return [key, when.toLocaleString()];
    }
    return [key, value];
  })));
}

const EMPTY: Values = {};
const input = "min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-(--fm-ink)";

// `base`: the admin console reads `/api/admin`; the organiser panel reads the
// points ledger from `/api/staff` (an administrator or a Game Master), without
// the CSV exports, which stay in the admin console.
export function AdminList({ kind, refreshKey, base = "/api/admin" }: { kind: ListKind; refreshKey: number; base?: LedgerBase }) {
  const { t } = useLanguage();
  const [draft, setDraft] = useState<Values>(EMPTY);
  const [applied, setApplied] = useState<Values>(EMPTY);
  // cursors[i] is the cursor that started page i (null for the first page).
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [retry, setRetry] = useState(0);
  const [formProblem, setFormProblem] = useState("");
  // The points entry being reversed: its confirmation screen replaces the list.
  const [reversing, setReversing] = useState<LedgerEntry | null>(null);
  // The answer, tagged with the request it answers. "Loading" is whatever is
  // left when the newest request has no answer yet - no state is set to say so.
  const [answer, setAnswer] = useState<{ key: string; load: Load } | null>(null);
  const cursor = cursors[cursors.length - 1] ?? null;
  const requestKey = JSON.stringify([base, kind, applied, cursor, refreshKey, retry]);
  const load: Load = answer !== null && answer.key === requestKey ? answer.load : { status: "loading" };

  useEffect(() => {
    let current = true;
    void apiGet<Page>(`${base}/${kind}${listQuery(kind, applied, cursor)}`).then((result) => {
      if (!current) return; // a newer load has started: this answer is stale
      setAnswer({
        key: requestKey,
        load: result.ok && Array.isArray(result.data?.items)
          ? { status: "ready", page: result.data }
          : { status: "error", message: result.ok ? messageFor("INTERNAL_ERROR") : errorMessage(result.error.code) },
      });
    });
    return () => { current = false; };
  }, [base, kind, applied, cursor, refreshKey, retry, requestKey]);

  function apply(event?: FormEvent) {
    event?.preventDefault();
    const found = problem(kind, draft);
    setFormProblem(found);
    if (found) return;
    setCursors([null]); // a new filter starts at the first page: old cursors do not fit it
    setApplied({ ...draft });
  }

  function reset() {
    setFormProblem("");
    setDraft(EMPTY);
    setCursors([null]);
    setApplied({});
  }

  const filtered = Object.values(applied).some((value) => value.trim() !== "");
  const ready = load.status === "ready" ? load.page : null;
  const busy = load.status === "loading";

  if (reversing !== null) {
    return <PointsReversal entry={reversing} base={base} onClose={(changed) => { setReversing(null); if (changed) setRetry((n) => n + 1); }} />;
  }

  return <div className="grid gap-4">
    <form className="grid gap-3 rounded-xl border border-(--fm-line) bg-(--fm-tint) p-4 sm:grid-cols-2" aria-label={t("Filters")} onSubmit={apply}>
      {FILTERS[kind].map((def) => <label key={def.key} className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t(def.label)}
        {def.type === "select"
          ? <select className={input} value={draft[def.key] ?? ""} onChange={(event) => setDraft({ ...draft, [def.key]: event.target.value })}>
            <option value="">{t("Any")}</option>
            {def.options?.map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
          </select>
          : <input className={input} type={def.type === "date" ? "date" : "text"} inputMode={def.type === "number" ? "numeric" : undefined}
            value={draft[def.key] ?? ""} onChange={(event) => setDraft({ ...draft, [def.key]: event.target.value })} />}
      </label>)}
      <p className="text-xs text-(--fm-muted) sm:col-span-2">{t("Dates use your local time zone and the end date is included.")}</p>
      {formProblem && <p role="alert" className="text-sm text-(--fm-warn) sm:col-span-2">{t(formProblem)}</p>}
      <div className="flex flex-wrap gap-2 sm:col-span-2">
        <button type="submit" className="min-h-11 rounded-xl bg-(--fm-red) text-white px-4 py-2 font-semibold">{t("Search")}</button>
        <button type="button" className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2" onClick={reset}>{t("Reset")}</button>
      </div>
    </form>

    {base === "/api/admin" && <ExportBar kind={kind} applied={applied} pending={!sameFilters(kind, draft, applied)} />}

    {busy && <p role="status" className="text-sm text-(--fm-ink-soft)">{t("Loading records…")}</p>}
    {load.status === "error" && <div className="grid gap-2">
      <p role="alert" className="text-(--fm-warn)">{t(load.message)}</p>
      <button className="min-h-11 w-fit rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2" onClick={() => setRetry((n) => n + 1)}>{t("Try again")}</button>
    </div>}
    {ready && <Records data={readable(ready.items)} empty={filtered ? "No records match these filters." : "No records to show yet."}
      actions={kind === "points-log" ? (_row, index) => {
        const entry = ready.items[index]; // the raw row: `readable` turned its dates into text
        return entry && canReverse(entry)
          ? <button type="button" className="mt-3 min-h-11 rounded-xl border border-(--fm-red) bg-(--fm-tint) px-4 py-2 text-sm font-semibold text-(--fm-red)"
            onClick={() => setReversing(entry as unknown as LedgerEntry)}>{t("Reverse…")}</button>
          : null;
      } : undefined} />}

    {(ready || cursors.length > 1) && <nav className="flex flex-wrap items-center gap-3" aria-label={t("Pages")}>
      <button className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2 disabled:opacity-50" disabled={busy || cursors.length === 1}
        onClick={() => setCursors((stack) => stack.slice(0, -1))}>{t("Previous")}</button>
      <span className="text-sm text-(--fm-ink-soft)">{t("Page")} {cursors.length}</span>
      <button className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2 disabled:opacity-50" disabled={busy || !ready?.next_cursor}
        onClick={() => { const next = ready?.next_cursor; if (next) setCursors((stack) => (stack[stack.length - 1] === next ? stack : [...stack, next])); }}>{t("Next")}</button>
    </nav>}
  </div>;
}
