"use client";

// The admin audit trail: a read-only, paginated, filterable list of what
// administrators changed and which CSV exports ran.
//
// Paging is server-side keyset paging, exactly like the other admin lists: the
// page carries an opaque `next_cursor`, "Previous" is a client-side stack of the
// cursors already used, and a new filter starts again at the first page (a
// cursor is bound to the filters it was issued for).
//
// Stale answers can never reach the screen: the loading effect owns a `current`
// flag that its cleanup clears, so any change that starts a newer load - a
// filter, a page move, the Refresh button, leaving the section - discards the
// reply of every older one.
//
// Nothing the server sends is rendered as markup: every value, name and message
// is a React text node. The server has already withheld secrets and personal
// data (an allowlist per kind of event); this component adds nothing back.
import { useEffect, useState, type FormEvent } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet } from "@/lib/api";
import {
  ACTION_OPTIONS,
  EXPORT_KIND_LABELS,
  FIELD_LABELS,
  FILTER_NAME_LABELS,
  NO_FILTERS,
  OUTCOME_LABELS,
  REASON_LABELS,
  TARGET_OPTIONS,
  VALUE_LABELS,
  actionLabel,
  ownLabel,
  anyAuditFilter,
  auditProblem,
  auditQuery,
  diffRows,
  sameAuditFilters,
  targetLabel,
  type AuditEvent,
  type AuditPageData,
  type AuditResult,
  type AuditValue,
  type AuditValues,
} from "@/lib/auditView";
import { messageFor } from "@/lib/errors";

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; page: AuditPageData };

const LOCALES = { az: "az-AZ", en: "en-GB", ru: "ru-RU" } as const;
const input = "min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-(--fm-ink)";
const button = "min-h-11 rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2 disabled:opacity-50";

function errorMessage(code: string): string {
  return code === "VALIDATION_ERROR" ? "The filters are not valid. Check the values and dates, then try again." : messageFor(code);
}

export function AuditLog({ refreshKey }: { refreshKey: number }) {
  const { t, language } = useLanguage();
  const [draft, setDraft] = useState<AuditValues>(NO_FILTERS);
  const [applied, setApplied] = useState<AuditValues>(NO_FILTERS);
  // cursors[i] is the cursor that started page i (null for the first page).
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [retry, setRetry] = useState(0);
  const [formProblem, setFormProblem] = useState("");
  const [openId, setOpenId] = useState<number | null>(null);
  // The answer, tagged with the request it answers. "Loading" is whatever is
  // left when the newest request has no answer yet - no state is set to say so.
  const [answer, setAnswer] = useState<{ key: string; load: Load } | null>(null);
  const cursor = cursors[cursors.length - 1] ?? null;
  const requestKey = JSON.stringify([applied, cursor, refreshKey, retry]);
  const load: Load = answer !== null && answer.key === requestKey ? answer.load : { status: "loading" };

  useEffect(() => {
    let current = true;
    void apiGet<AuditPageData>(`/api/admin/audit${auditQuery(applied, cursor)}`).then((result) => {
      if (!current) return; // a newer load has started: this answer is stale
      setAnswer({
        key: requestKey,
        load: result.ok && Array.isArray(result.data?.items)
          ? { status: "ready", page: result.data }
          : { status: "error", message: result.ok ? messageFor("INTERNAL_ERROR") : errorMessage(result.error.code) },
      });
    });
    return () => { current = false; };
  }, [applied, cursor, refreshKey, retry, requestKey]);

  function apply(event?: FormEvent) {
    event?.preventDefault();
    const found = auditProblem(draft);
    setFormProblem(found);
    if (found) return;
    setCursors([null]); // a new filter starts at the first page: old cursors do not fit it
    setOpenId(null);
    setApplied({ ...draft });
  }

  function reset() {
    setFormProblem("");
    setDraft(NO_FILTERS);
    setCursors([null]);
    setOpenId(null);
    setApplied({});
  }

  // Shows every event of one operation (an export's started / finished / refused).
  function showOperation(operationId: string) {
    const next: AuditValues = { target_type: "export", target_id: operationId };
    setFormProblem("");
    setDraft(next);
    setCursors([null]);
    setOpenId(null);
    setApplied(next);
  }

  const ready = load.status === "ready" ? load.page : null;
  const busy = load.status === "loading";
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  function when(iso: string): string {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(LOCALES[language], { dateStyle: "medium", timeStyle: "medium" });
  }

  function value(field: string, shown: AuditValue | undefined): string {
    if (shown === undefined) return t("Not recorded");
    if (shown === null || shown === "") return "—";
    if (typeof shown === "boolean") return t(shown ? "Yes" : "No");
    const words = Object.prototype.hasOwnProperty.call(VALUE_LABELS, field) ? VALUE_LABELS[field] : undefined;
    const word = words !== undefined && typeof shown === "string" ? ownLabel(words, shown) : null;
    return word !== null ? t(word) : String(shown);
  }

  function labelled(map: Record<string, string>, key: string | null): string {
    if (key === null) return "—";
    const known = ownLabel(map, key);
    return known !== null ? t(known) : key; // an unknown code is shown as plain text
  }

  function count(shown: number | null): string {
    return shown === null ? "—" : shown.toLocaleString(LOCALES[language]);
  }

  function targetText(event: AuditEvent): string {
    const kind = targetLabel(event.target_type);
    const type = kind === null ? event.target_type : t(kind);
    return event.target_id === null ? type : `${type} #${event.target_id}`;
  }

  // The one line an export event adds to its row: how it went, in numbers.
  function exportSummary(result: AuditResult): string {
    const parts = [labelled(OUTCOME_LABELS, result.outcome)];
    if (result.rows_written !== null && result.rows_expected !== null) parts.push(`${count(result.rows_written)} / ${count(result.rows_expected)} ${t("rows")}`);
    else if (result.rows_expected !== null) parts.push(`${count(result.rows_expected)} ${t("rows")}`);
    else if (result.rows_matched !== null) parts.push(`${count(result.rows_matched)} ${t("rows")}`);
    return parts.join(" · ");
  }

  function detail(event: AuditEvent) {
    const rows = diffRows(event);
    const result = event.result;
    return <div id={`audit-detail-${event.id}`} className="mt-3 grid gap-3 border-t border-(--fm-line) pt-3" aria-label={`${t("Details")} #${event.id}`}>
      <dl className="grid gap-2 sm:grid-cols-2">
        <div className="min-w-0"><dt className="text-xs font-bold uppercase tracking-wide text-(--fm-red)">{t("Event kind")}</dt><dd className="break-words text-sm text-(--fm-ink)">{event.action}</dd></div>
        {event.operation_id !== null && <div className="min-w-0"><dt className="text-xs font-bold uppercase tracking-wide text-(--fm-red)">{t("Operation ID")}</dt><dd className="break-all text-sm text-(--fm-ink)">{event.operation_id}</dd></div>}
      </dl>

      {result !== null && <dl className="grid gap-2 sm:grid-cols-2" aria-label={t("Result")}>
        {([
          ["Outcome", labelled(OUTCOME_LABELS, result.outcome)],
          ["Reason", result.reason === null ? "—" : labelled(REASON_LABELS, result.reason)],
          ["Error type", result.error_class ?? "—"],
          ["Rows expected", count(result.rows_expected)],
          ["Rows sent", count(result.rows_written)],
          ["Rows matched", count(result.rows_matched)],
          ["Row limit", count(result.max_rows)],
          ["Bytes sent", count(result.bytes_written)],
          ["Duration (ms)", count(result.elapsed_ms)],
          ["Data snapshot taken", result.snapshot_at === null ? "—" : when(result.snapshot_at)],
          ["Filters used", result.filters_used.length ? result.filters_used.map((name) => labelled(FILTER_NAME_LABELS, name)).join(", ") : t("no filters")],
        ] as [string, string][]).map(([label, shown]) => <div key={label} className="min-w-0">
          <dt className="text-xs font-bold uppercase tracking-wide text-(--fm-red)">{t(label)}</dt>
          <dd className="break-words text-sm text-(--fm-ink)">{shown}</dd>
        </div>)}
        {result.outcome === "stream_finished" && <p className="text-xs text-(--fm-muted) sm:col-span-2">{t("The server records what it sent, not whether the browser saved the file.")}</p>}
      </dl>}

      {rows.length > 0 && <div className="overflow-x-auto">
        <table className="w-full min-w-[20rem] text-left text-sm">
          <thead><tr className="text-xs uppercase tracking-wide text-(--fm-red)">
            <th scope="col" className="py-1 pr-3">{t("Field")}</th>
            <th scope="col" className="py-1 pr-3">{t("Before")}</th>
            <th scope="col" className="py-1">{t("After")}</th>
          </tr></thead>
          <tbody>{rows.map((row) => <tr key={row.field} className={row.changed ? "bg-(--fm-warn-soft)" : undefined}>
            <th scope="row" className="py-1 pr-3 align-top font-medium text-(--fm-ink)">
              {labelled(FIELD_LABELS, row.field)}
              {row.changed && <span className="ml-2 rounded border border-(--fm-warn-line) px-1 text-[0.65rem] font-semibold uppercase text-(--fm-warn)">{t("Changed")}</span>}
            </th>
            <td className="break-words py-1 pr-3 align-top text-(--fm-ink-soft)">{value(row.field, row.before)}</td>
            <td className="break-words py-1 align-top text-(--fm-ink)">{value(row.field, row.after)}</td>
          </tr>)}</tbody>
        </table>
      </div>}

      {event.details === "limited" && <p className="text-sm text-(--fm-ink-soft)">{t("This kind of event is not recognised by this page, so only who, when and what it concerned are shown.")}</p>}
      {event.hidden_field_count > 0 && <p className="text-xs text-(--fm-muted)">{t("Some fields are hidden because they are personal or not needed here")}: {event.hidden_field_count}</p>}

      {event.operation_id !== null && !(applied.target_type === "export" && applied.target_id === event.operation_id) &&
        <button type="button" className={`${button} w-fit`} onClick={() => showOperation(event.operation_id as string)}>{t("Show the events of this export")}</button>}
      {event.operation_id !== null && applied.target_type === "export" && applied.target_id === event.operation_id &&
        <p className="text-xs text-(--fm-muted)">{t("This export's events are shown.")}</p>}
    </div>;
  }

  return <div className="grid gap-4">
    <p className="text-sm text-(--fm-ink-soft)">{t("Read-only trail of administrator changes and CSV exports. Viewing it records nothing.")}</p>
    <form className="grid gap-3 rounded-xl border border-(--fm-line) bg-(--fm-tint) p-4 sm:grid-cols-2" aria-label={t("Audit log filters")} onSubmit={apply}>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Staff ID")}
        <input className={input} type="text" inputMode="numeric" value={draft.actor_id ?? ""} onChange={(event) => setDraft({ ...draft, actor_id: event.target.value })} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Action")}
        <select className={input} value={draft.action ?? ""} onChange={(event) => setDraft({ ...draft, action: event.target.value })}>
          <option value="">{t("Any")}</option>
          {ACTION_OPTIONS.map(([code, label]) => <option key={code} value={code}>{t(label)}</option>)}
        </select>
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Target type")}
        <select className={input} value={draft.target_type ?? ""} onChange={(event) => setDraft({ ...draft, target_type: event.target.value })}>
          <option value="">{t("Any")}</option>
          {TARGET_OPTIONS.map(([code, label]) => <option key={code} value={code}>{t(label)}</option>)}
        </select>
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Target ID")}
        <input className={input} type="text" value={draft.target_id ?? ""} onChange={(event) => setDraft({ ...draft, target_id: event.target.value })} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("From date")}
        <input className={input} type="date" value={draft.created_from ?? ""} onChange={(event) => setDraft({ ...draft, created_from: event.target.value })} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Until date")}
        <input className={input} type="date" value={draft.created_to ?? ""} onChange={(event) => setDraft({ ...draft, created_to: event.target.value })} />
      </label>
      <p className="text-xs text-(--fm-muted) sm:col-span-2">{t("Dates use your local time zone and the end date is included.")}</p>
      {formProblem && <p role="alert" className="text-sm text-(--fm-warn) sm:col-span-2">{t(formProblem)}</p>}
      <div className="flex flex-wrap gap-2 sm:col-span-2">
        <button type="submit" className="min-h-11 rounded-xl bg-(--fm-red) text-white px-4 py-2 font-semibold">{t("Search")}</button>
        <button type="button" className={button} onClick={reset}>{t("Reset")}</button>
      </div>
    </form>

    <p className="text-xs text-(--fm-muted)">{t("Times are shown in your local time zone")}: {timeZone}</p>

    {busy && <p role="status" className="text-sm text-(--fm-ink-soft)">{t("Loading audit events…")}</p>}
    {load.status === "error" && <div className="grid gap-2">
      <p role="alert" className="text-(--fm-warn)">{t(load.message)}</p>
      <button className={`${button} w-fit`} onClick={() => setRetry((n) => n + 1)}>{t("Try again")}</button>
    </div>}
    {ready && (ready.items.length === 0
      ? <p className="rounded-xl border border-(--fm-line) p-5 text-sm text-(--fm-ink-soft)">{t(anyAuditFilter(applied) ? "No audit events match these filters." : "No audit events recorded yet.")}</p>
      : <ul className="grid gap-3" aria-label={t("Audit events")}>{ready.items.map((event) => {
        const open = openId === event.id;
        const kind = actionLabel(event.action);
        return <li key={event.id} className="min-w-0 rounded-xl border border-(--fm-line) bg-(--fm-tint) p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className="break-words font-semibold text-(--fm-ink)">{kind === null ? event.action : t(kind)}</p>
            <time dateTime={event.created_at} className="text-xs text-(--fm-muted)">{when(event.created_at)}</time>
          </div>
          <p className="mt-1 break-words text-sm text-(--fm-ink-soft)">{t("Actor")}: <span>{`${event.actor_name ?? "—"} (#${event.actor_id})`}</span> · {t("Target")}: <span>{targetText(event)}</span></p>
          {event.result !== null && <p className="mt-1 break-words text-sm text-(--fm-ink)">{exportSummary(event.result)}</p>}
          <button type="button" className={`${button} mt-3`} aria-expanded={open} aria-controls={`audit-detail-${event.id}`}
            onClick={() => setOpenId(open ? null : event.id)}>{t(open ? "Hide details" : "Details")}</button>
          {open && detail(event)}
        </li>;
      })}</ul>)}

    {(ready || cursors.length > 1) && <nav className="flex flex-wrap items-center gap-3" aria-label={t("Pages")}>
      <button className={button} disabled={busy || cursors.length === 1}
        onClick={() => { setOpenId(null); setCursors((stack) => stack.slice(0, -1)); }}>{t("Previous")}</button>
      <span className="text-sm text-(--fm-ink-soft)">{t("Page")} {cursors.length}</span>
      <button className={button} disabled={busy || !ready?.next_cursor}
        onClick={() => { const next = ready?.next_cursor; if (next) { setOpenId(null); setCursors((stack) => (stack[stack.length - 1] === next ? stack : [...stack, next])); } }}>{t("Next")}</button>
    </nav>}
  </div>;
}

export { EXPORT_KIND_LABELS, sameAuditFilters };
