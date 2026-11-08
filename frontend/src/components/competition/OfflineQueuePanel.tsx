"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import { claim, discard, durableQueueSupported, flush, queueState, type FlushReport, type Owner, type QueueState } from "@/lib/offlineQueue";
import { confirmAction } from "@/lib/confirm";

const TICK_MS = 30_000;

type Line = { tone: "ok" | "warn"; text: string };

function describe(report: FlushReport, t: (s: string) => string): Line[] {
  return report.answered.map(({ result }) => {
    if (!result.ok) return { tone: "warn", text: `${t("A queued scan was refused:")} ${t(messageFor(result.error.code))}` };
    const { status, outcome } = result.data;
    const who = outcome?.name ? `${outcome.name} · ` : "";
    if (status === "applied") return { tone: "ok", text: `${who}${t("Queued scan accepted.")}` };
    if (status === "pending_review") return { tone: "ok", text: t("Queued scan sent for review: it counts only if a Game Master approves it.") };
    return { tone: "warn", text: t("A queued scan was not accepted.") };
  });
}

// What this phone still has to send for THIS account, and what it holds for
// others. It replays on reconnection, every 30 s while open and on demand -
// never in a tight loop - and stops (keeping the queue) when the session needs
// a new sign-in. Scans of another account, an older dataset or an older version
// of the queue are never sent automatically, and never dropped without asking.
export function OfflineQueuePanel({ owner: given }: { owner: Owner }) {
  const { t } = useLanguage();
  // Stable across re-renders: the identity is the label and the dataset.
  const { queue_owner: label, dataset_generation: generation, name } = given;
  const owner = useMemo<Owner>(() => ({ queue_owner: label, dataset_generation: generation, name }), [label, generation, name]);
  const [state, setState] = useState<QueueState>({ mine: [], quarantined: [], expired: [] });
  const [lines, setLines] = useState<Line[]>([]);
  const [auth, setAuth] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [storageFailed, setStorageFailed] = useState(false);
  const [unsupported, setUnsupported] = useState(false);
  const running = useRef(false);

  const send = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      const report = await flush(owner);
      setAuth(report.authNeeded);
      setRetrying(report.offline);
      setStorageFailed(report.storageFailed);
      setUnsupported(report.unsupported);
      if (report.answered.length) setLines(describe(report, t));
      setState(await queueState(owner));
    } finally {
      running.current = false;
    }
  }, [owner, t]);

  useEffect(() => {
    const refresh = () => void queueState(owner).then(setState);
    const initial = window.setTimeout(() => void send(), 0);
    const timer = window.setInterval(() => void send(), TICK_MS);
    const online = () => void send();
    window.addEventListener("online", online);
    window.addEventListener("facemark:offline-queue", refresh);
    window.addEventListener("storage", refresh); // another tab changed the queue
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      window.removeEventListener("online", online);
      window.removeEventListener("facemark:offline-queue", refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [owner, send]);

  const unfiled = state.quarantined.filter((s) => s.owner === undefined);
  const others = state.quarantined.filter((s) => s.owner !== undefined);
  const decide = (run: Promise<{ saved: boolean }>, then?: () => void) =>
    void run.then((r) => {
      if (!r.saved) setStorageFailed(true); // nothing was changed: say so
      void queueState(owner).then(setState);
      if (r.saved && then) then();
    });
  if (unsupported || !durableQueueSupported()) {
    return (
      <p role="alert" className="fm-alert fm-alert-warn" data-testid="offline-unsupported">
        {t("This browser cannot keep scans safely while offline. Keep the connection, or update the browser (iOS 15.4 or newer).")}
      </p>
    );
  }
  if (state.mine.length === 0 && state.quarantined.length === 0 && state.expired.length === 0 && lines.length === 0 && !auth && !storageFailed) return null;
  return (
    <section className="fm-card fm-section" aria-labelledby="offline-queue">
      <h2 id="offline-queue" className="fm-eyebrow">{t("Scans waiting to be sent")}</h2>
      {state.mine.length > 0 && <p className="fm-strong" data-testid="offline-count">{t("{n} on this phone").replace("{n}", String(state.mine.length))}</p>}
      {retrying && state.mine.length > 0 && <p className="fm-small">{t("The server could not take them yet; they are retried automatically.")}</p>}
      {auth && (
        <p role="alert" className="fm-alert fm-alert-warn">
          {t("Your session has ended. Sign in again to send the saved scans; they are kept on this phone.")}{" "}
          <Link href="/staff/login">{t("Sign in")}</Link>
        </p>
      )}
      {storageFailed && <p role="alert" className="fm-alert fm-alert-warn">{t("This phone could not update its saved scans. Nothing was lost; free some space or reload, then try again.")}</p>}
      {state.mine.length > 0 && <button type="button" className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => void send()}>{t("Send now")}</button>}

      {others.length > 0 && (
        <div className="fm-alert fm-alert-warn" data-testid="offline-quarantine-others">
          <p>{t("{n} scans on this phone belong to another account or to an earlier event dataset. They are not sent as yours.").replace("{n}", String(others.length))}</p>
          <p className="fm-small">{t("Sign in with the account that made them to send them.")} {others[0]?.owner?.name ? `(${others[0].owner.name})` : ""}</p>
          <button type="button" className="fm-btn fm-btn-quiet" onClick={() => {
            void confirmAction(t("Delete these scans from this phone? They were never sent.")).then((ok) => { if (ok) decide(discard(others.map((s) => s.client_scan_id))); });
          }}>{t("Delete them from this phone")}</button>
        </div>
      )}
      {unfiled.length > 0 && (
        <div className="fm-alert fm-alert-warn" data-testid="offline-quarantine-unfiled">
          <p>{t("{n} scans were saved by an older version of this page, without the account that made them.").replace("{n}", String(unfiled.length))}</p>
          <button type="button" className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => {
            void confirmAction(t("Send them as your scans? Only do this if you made them on this phone.")).then((ok) => { if (ok) decide(claim(unfiled.map((s) => s.client_scan_id), owner), () => void send()); });
          }}>{t("I made them: send as mine")}</button>
          <button type="button" className="fm-btn fm-btn-quiet" onClick={() => {
            void confirmAction(t("Delete these scans from this phone? They were never sent.")).then((ok) => { if (ok) decide(discard(unfiled.map((s) => s.client_scan_id))); });
          }}>{t("Delete them from this phone")}</button>
        </div>
      )}
      {state.expired.length > 0 && (
        <div className="fm-alert fm-alert-warn" data-testid="offline-expired">
          <p>{t("{n} scans on this phone are older than a day and are no longer sent automatically.").replace("{n}", String(state.expired.length))}</p>
          <button type="button" className="fm-btn fm-btn-quiet" onClick={() => {
            void confirmAction(t("Delete these scans from this phone? They were never sent.")).then((ok) => { if (ok) decide(discard(state.expired.map((s) => s.client_scan_id))); });
          }}>{t("Delete them from this phone")}</button>
        </div>
      )}
      {lines.map((l, i) => (
        <p key={i} role={l.tone === "ok" ? "status" : "alert"} className={`fm-alert ${l.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{l.text}</p>
      ))}
    </section>
  );
}
