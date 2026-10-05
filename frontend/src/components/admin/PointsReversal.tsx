"use client";

// The confirmation screen for reversing one points entry. It is a screen of its
// own (the ledger list is not shown beside it), says exactly what will happen -
// including what a reversal does NOT do - and needs a reason before the button
// works.
//
// It claims nothing before the server has answered. A retry after a lost answer
// reuses the SAME idempotency key (while the reason is unchanged), so a request
// that did reach the server can never reverse the entry twice: the server replays
// its first answer. Changing the reason makes a new request.
import { useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import {
  REASON_MAX,
  type LedgerEntry,
  type ReversalResult,
  newIdempotencyKey,
  normalizeReason,
  reasonProblem,
  reverseEntry,
} from "@/lib/reversal";

const LOCALES = { az: "az-AZ", en: "en-GB", ru: "ru-RU" } as const;
const button = "min-h-11 rounded-xl border border-slate-600 bg-slate-800 px-4 py-2 disabled:opacity-50";
// Reversal codes that mean the entry itself is no longer reversible: the only
// useful next step is to go back and look at the ledger again.
const ENTRY_GONE = new Set(["POINTS_ALREADY_REVERSED", "POINTS_NOT_REVERSIBLE", "POINTS_ENTRY_NOT_FOUND"]);

export function PointsReversal({ entry, onClose }: { entry: LedgerEntry; onClose: (changed: boolean) => void }) {
  const { t, language } = useLanguage();
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [result, setResult] = useState<ReversalResult | null>(null);
  const inFlight = useRef(false);
  const attempt = useRef<{ reason: string; key: string } | null>(null);

  async function submit() {
    if (inFlight.current) return; // a double click is one request
    const found = reasonProblem(reason);
    setProblem(found);
    if (found) return;
    const normalized = normalizeReason(reason);
    if (attempt.current?.reason !== normalized) attempt.current = { reason: normalized, key: newIdempotencyKey() };
    inFlight.current = true;
    setBusy(true);
    setErrorCode(null);
    const answer = await reverseEntry(entry.id, normalized, attempt.current.key);
    inFlight.current = false;
    setBusy(false);
    if (answer.ok) setResult(answer.data);
    else setErrorCode(answer.error.code);
  }

  const when = (() => {
    const date = new Date(entry.created_at);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(LOCALES[language]);
  })();

  if (result !== null) {
    return <div className="grid gap-4" role="status">
      <h3 className="text-lg font-semibold">{t("Points reversed")}</h3>
      <p className="text-sm text-emerald-200">
        {result.points_reversed} {t("were taken away. The attendee's total is now")} {result.resulting_total}.
      </p>
      <p className="text-sm text-slate-300">{t("Reversal entry")}: #{result.reversal_id}</p>
      {result.replayed && <p className="text-sm text-slate-300">{t("This reversal had already been recorded; nothing was changed a second time.")}</p>}
      <button type="button" className={`${button} w-fit`} onClick={() => onClose(true)}>{t("Back to the ledger")}</button>
    </div>;
  }

  const gone = errorCode !== null && ENTRY_GONE.has(errorCode);
  const lost = errorCode === "NETWORK_ERROR";
  const reasonLength = normalizeReason(reason).length;

  return <section className="grid gap-4" aria-label={t("Reverse points")}>
    <h3 className="text-lg font-semibold">{t("Reverse points")}</h3>
    <p className="text-sm text-slate-300">{t("Review this entry before you reverse it.")}</p>
    <dl className="grid gap-2 rounded-xl border border-slate-700 bg-slate-950/50 p-4 sm:grid-cols-2">
      {([
        ["Entry", `#${entry.id}`],
        ["Attendee", entry.attendee_name],
        ["Booth", entry.booth_name ?? "—"],
        ["Activity", entry.activity],
        ["Points", `+${entry.points}`],
        ["Date", when],
      ] as [string, string][]).map(([label, value]) => <div key={label} className="min-w-0">
        <dt className="text-xs font-bold uppercase tracking-wide text-cyan-300">{t(label)}</dt>
        <dd className="break-words text-sm text-slate-100">{value}</dd>
      </div>)}
    </dl>

    <div className="grid gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100">
      <p className="font-semibold">{t("What happens")}</p>
      <ul className="grid list-disc gap-1 pl-5">
        <li>{t("The attendee's total goes down by the points of this entry.")}</li>
        <li>{t("The original entry stays in the ledger. A new negative entry is added and linked to it, with your name and the reason.")}</li>
        <li>{t("The leaderboard and the attendee's score update.")}</li>
        <li>{t("This cannot be undone here: a reversal cannot itself be reversed. A mistaken reversal is corrected by awarding the points again.")}</li>
        <li className="font-semibold">{t(entry.source === "FEEDBACK"
          ? "This does NOT let this attendee earn the feedback reward at this booth again."
          : "This does NOT make the activity available to this attendee again if it can be earned only once. Activities that allow repeats can still be earned again.")}</li>
      </ul>
    </div>

    {!gone && <label className="flex flex-col gap-1 text-sm text-slate-300">{t("Reason (required, 3–300 characters)")}
      <textarea className="min-h-24 w-full rounded-xl border border-slate-600 bg-slate-900 p-3 text-white" rows={3} maxLength={REASON_MAX * 2}
        value={reason} disabled={busy} aria-invalid={problem ? true : undefined}
        onChange={(event) => { setReason(event.target.value); if (problem) setProblem(""); }} />
      <span className={`text-xs ${reasonLength > REASON_MAX ? "text-amber-300" : "text-slate-400"}`}>{reasonLength}/{REASON_MAX} {t("characters")}</span>
    </label>}
    {problem && <p role="alert" className="text-sm text-amber-300">{t(problem)}</p>}

    {errorCode !== null && <div role="alert" className="grid gap-1 rounded-xl border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200">
      <p>{t(messageFor(errorCode))}</p>
      {lost && <p>{t("We could not tell whether the reversal was recorded. Trying again is safe: the same request can never reverse the entry twice.")}</p>}
    </div>}

    <div className="flex flex-wrap gap-2">
      {gone
        ? <button type="button" className={button} onClick={() => onClose(true)}>{t("Back to the ledger")}</button>
        : <>
          <button type="button" disabled={busy || reasonLength === 0}
            className="min-h-11 rounded-xl bg-red-600 px-4 py-2 font-semibold text-white disabled:opacity-50"
            onClick={() => void submit()}>
            {busy ? t("Reversing…") : lost ? t("Try again") : `${t("Reverse these points")} (${entry.points})`}
          </button>
          <button type="button" disabled={busy} className={button} onClick={() => onClose(false)}>{t("Cancel")}</button>
        </>}
    </div>
  </section>;
}
