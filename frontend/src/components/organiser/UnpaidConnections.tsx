"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiPost } from "@/lib/api";
import { messageFor } from "@/lib/errors";

type Item = {
  connection_id: number;
  user_id: number;
  name: string;
  with: string;
  kind: "match" | "other";
  points: number;
  established_at: string;
  category: string;
};
type Review = { items: Item[]; counts: Record<string, number>; applicable: string };
type Result = { connection_id: number; user_id: number; result: string; points?: number };
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; review: Review };

// What each category means (backend people/reconcile.py).
export const CATEGORY_TEXT: Record<string, string> = {
  eligible: "Can be paid: connected on the event day, before the close, by a competing person.",
  needs_decision: "Needs an organiser decision: connected before the event day.",
  not_configured: "Cannot be decided yet: the competition close is not set.",
  same_company: "No points by rule: same company.",
  ineligible: "No points: the person is not part of the competition.",
  after_close: "No points: connected after the close.",
};

const key = (i: { connection_id: number; user_id: number }) => `${i.connection_id}:${i.user_id}`;

// Connections that never paid CONNECTION points (e.g. made before the close was
// configured). Reading changes nothing. Paying needs a selection, a reason and an
// explicit confirmation; the server pays only what is still eligible, through the
// ledger (once per pair and person, best four, the close), and audits each one.
export function UnpaidConnections() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [results, setResults] = useState<Result[] | null>(null);
  const [reviewNeeded, setReviewNeeded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    const r = await apiGet<Review>("/api/admin/connections/unpaid");
    setState(r.ok ? { kind: "ok", review: r.data } : { kind: "error", text: messageFor(r.error.code) });
  }, []);
  useEffect(() => {
    let active = true;
    void apiGet<Review>("/api/admin/connections/unpaid").then((r) => {
      if (active) setState(r.ok ? { kind: "ok", review: r.data } : { kind: "error", text: messageFor(r.error.code) });
    });
    return () => { active = false; };
  }, []);

  async function apply(items: Item[]) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(""); setResults(null);
    const r = await apiPost<{ results: Result[]; final_review_needed?: boolean }>("/api/admin/connections/reconcile", {
      items: items.map((i) => ({ connection_id: i.connection_id, user_id: i.user_id })),
      reason: reason.trim(),
      confirm: true,
    });
    inFlight.current = false;
    setBusy(false);
    if (!r.ok) { setError(messageFor(r.error.code)); return; }
    setResults(r.data.results);
    setReviewNeeded(r.data.final_review_needed === true);
    setPicked(new Set()); setConfirmed(false);
    await load();
  }

  const review = state.kind === "ok" ? state.review : null;
  const items = review?.items ?? [];
  const chosen = items.filter((i) => picked.has(key(i)));
  return (
    <section className="fm-card fm-section" aria-labelledby="org-unpaid">
      <h3 id="org-unpaid" className="fm-eyebrow">{t("Connections without points")}</h3>
      <p className="fm-small">{t("Reading this list changes nothing. A later scan never pays these connections; pay one here only when the facts allow it.")}</p>
      {state.kind === "loading" && <p role="status" className="fm-note">{t("Loading…")}</p>}
      {state.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>}
      {review && items.length === 0 && <p className="fm-empty">{t("Every connection holds the points it earned.")}</p>}
      {review && items.length > 0 && (
          <>
            <ul className="fm-small fm-org-rules" aria-label={t("Why")}>
              {Object.entries(review.counts).map(([c, n]) => <li key={c}>{n} · {t(CATEGORY_TEXT[c] ?? c)}</li>)}
            </ul>
            <ul className="fm-list" aria-label={t("Connections without points")}>
              {items.map((i) => {
                const can = i.category === review.applicable;
                return (
                  <li key={key(i)} className="fm-small">
                    <label className="fm-remember">
                      <input type="checkbox" disabled={!can || busy} checked={picked.has(key(i))}
                        onChange={(e) => setPicked((now) => {
                          const next = new Set(now);
                          if (e.target.checked) next.add(key(i)); else next.delete(key(i));
                          return next;
                        })} />
                      <span>{i.name} · {i.with} · {t(i.kind === "match" ? "recommended" : "ordinary")} ({i.points}) · {t(CATEGORY_TEXT[i.category] ?? i.category)}</span>
                    </label>
                  </li>
                );
              })}
            </ul>
            <label className="fm-field">
              <span className="fm-label">{t("Reason (required, 3–300 characters)")}</span>
              <input className="fm-input" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} />
            </label>
            <label className="fm-remember">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
              <span>{t("I checked these connections and confirm the payment through the normal rules.")}</span>
            </label>
            <button type="button" className="fm-btn fm-btn-primary" disabled={busy || chosen.length === 0 || !confirmed || reason.trim().length < 3}
              onClick={() => void apply(chosen)}>
              {t("Pay the selected connections ({n})").replace("{n}", String(chosen.length))}
            </button>
          </>
      )}
      {error && <p role="alert" className="fm-alert fm-alert-warn">{t(error)}</p>}
      {reviewNeeded && <p role="alert" className="fm-alert fm-alert-warn">{t("Points changed after a final snapshot. Take a new snapshot in Final results, review it and publish that one.")}</p>}
      {results && (
        <ul className="fm-small fm-org-rules" role="status" aria-label={t("Result")}>
          {results.map((r) => <li key={key(r)}>#{r.connection_id} · {t(RESULT_TEXT[r.result] ?? CATEGORY_TEXT[r.result] ?? r.result)}{r.points ? ` · +${r.points}` : ""}</li>)}
        </ul>
      )}
    </section>
  );
}

const RESULT_TEXT: Record<string, string> = {
  awarded: "Paid.",
  already_paid: "Already paid; nothing was added twice.",
  limit_reached: "Not paid: the best four connections already count more.",
  not_found: "Not found.",
};
