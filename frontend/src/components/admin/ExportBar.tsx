"use client";

// CSV export buttons for the admin lists.
//
// An export is two steps (see backend app/modules/admin/export_tickets.py):
//   1. POST the applied filters; the server validates them, checks the size
//      limit and answers with a one-time download link - or a readable error;
//   2. the page points a hidden frame at that link, and the BROWSER streams the
//      file straight to disk. The file is never read into page memory or a Blob.
//
// The frame hides the download's HTTP response from this page, so a refused or
// broken download would be invisible. The page therefore ASKS THE SERVER what
// became of it (`GET /api/admin/exports/status/<ref>`, admin-only, outcome and
// counts only - never CSV content) and shows only what it knows:
//
//   prepared          the server accepted the filters and issued a link
//   download started  the page pointed the frame at the link (the server has not
//                     answered yet - or the browser never asked)
//   server state      starting / streaming / finished / interrupted / failed /
//                     refused (with the reason and what a retry needs)
//
// What it can NOT know is whether the file reached the disk: "finished" means
// the server wrote the whole response, nothing more, and the text says so.
import { useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/ui/LanguageProvider";

import { apiGet, apiPost } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { exportBody, type ListKind, type Values } from "./listFilters";

type Prepared = { download_url: string; ref: string; status_url: string; rows: number; max_rows: number };
type ServerState = "ready" | "starting" | "streaming" | "finished" | "interrupted" | "failed" | "rejected" | "expired";
type Retry = "none" | "same_link" | "new_export";
type Status = {
  state: ServerState;
  code: string | null;
  retry: Retry;
  rows_expected: number | null;
  rows_written: number | null;
};
type Which = "list" | "sponsor";
// `check_status` is local: the download may be fine, the page just could not ask.
type Problem = { code: string; retry: Retry | "check_status"; written?: number | null; expected?: number | null };
type View =
  | { phase: "idle" }
  | { phase: "preparing"; which: Which }
  | { phase: "waiting"; rows: number }
  | { phase: "starting" }
  | { phase: "streaming"; written: number | null; expected: number | null }
  | { phase: "finished"; rows: number | null }
  | { phase: "problem"; problem: Problem };

export const POLL_MS = 1000;
// Still "ready" this long after the frame was pointed at the link: the browser
// never asked for the file (downloads blocked, frame refused...).
export const NOT_STARTED_AFTER_MS = 15_000;
// The server stops every export well before this (EXPORT_MAX_SECONDS).
export const GIVE_UP_AFTER_MS = 10 * 60 * 1000;
export const MAX_STATUS_FAILURES = 3;

let frame: HTMLIFrameElement | null = null;

// One hidden frame, kept for the life of the page: removing it could cancel a
// download that is still in progress.
function startDownload(url: string): void {
  if (frame === null || !frame.isConnected) {
    frame = document.createElement("iframe");
    frame.hidden = true;
    frame.setAttribute("aria-hidden", "true");
    frame.setAttribute("tabindex", "-1");
    frame.title = "";
    document.body.appendChild(frame);
  }
  frame.src = url;
}

function errorText(code: string): string {
  return code === "VALIDATION_ERROR" ? "The filters are not valid. Check the values and dates, then try again." : messageFor(code);
}

const BUSY_PHASES = new Set<View["phase"]>(["preparing", "waiting", "starting", "streaming"]);

export function ExportBar({ kind, applied, pending }: { kind: ListKind; applied: Values; pending: boolean }) {
  const { t } = useLanguage();
  const [view, setView] = useState<View>({ phase: "idle" });
  const lock = useRef(false); // set synchronously: two clicks in one tick cannot both pass
  const generation = useRef(0); // a newer export / retry / unmount makes every older poll stop
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const job = useRef<{ which: Which; downloadUrl: string; statusUrl: string } | null>(null);

  useEffect(() => () => {
    generation.current += 1;
    if (timer.current) clearTimeout(timer.current);
  }, []);

  function finish(next: View) {
    lock.current = false;
    setView(next);
  }

  function watch(statusUrl: string) {
    const mine = ++generation.current;
    if (timer.current) clearTimeout(timer.current);
    const startedAt = Date.now();
    let failures = 0;

    const poll = async (): Promise<void> => {
      if (mine !== generation.current) return;
      const result = await apiGet<Status>(statusUrl);
      if (mine !== generation.current) return; // a newer export, a retry, or the page was left
      if (!result.ok) {
        const { code } = result.error;
        if (code === "UNAUTHENTICATED" || code === "STAFF_NOT_AUTHORIZED") return finish({ phase: "problem", problem: { code: "UNAUTHENTICATED", retry: "none" } });
        if (result.status === 404) return finish({ phase: "problem", problem: { code: "EXPORT_STATUS_UNKNOWN", retry: "new_export" } });
        failures += 1;
        if (failures >= MAX_STATUS_FAILURES) return finish({ phase: "problem", problem: { code: "EXPORT_STATUS_UNREACHABLE", retry: "check_status" } });
        timer.current = setTimeout(() => void poll(), POLL_MS);
        return;
      }
      failures = 0;
      const status = result.data;
      const waited = Date.now() - startedAt;
      switch (status.state) {
        case "ready":
          if (waited > NOT_STARTED_AFTER_MS) return finish({ phase: "problem", problem: { code: "EXPORT_NOT_STARTED", retry: "same_link" } });
          break;
        case "starting":
          setView({ phase: "starting" });
          break;
        case "streaming":
          setView({ phase: "streaming", written: status.rows_written, expected: status.rows_expected });
          break;
        case "finished":
          return finish({ phase: "finished", rows: status.rows_written });
        case "interrupted":
        case "failed":
          return finish({ phase: "problem", problem: { code: status.code ?? "EXPORT_ABORTED", retry: status.retry, written: status.rows_written, expected: status.rows_expected } });
        case "rejected":
        case "expired":
          return finish({ phase: "problem", problem: { code: status.code ?? "INTERNAL_ERROR", retry: status.retry } });
      }
      if (waited > GIVE_UP_AFTER_MS) return finish({ phase: "problem", problem: { code: "EXPORT_STATUS_UNKNOWN", retry: "new_export" } });
      timer.current = setTimeout(() => void poll(), POLL_MS);
    };
    timer.current = setTimeout(() => void poll(), POLL_MS);
  }

  async function run(which: Which) {
    if (lock.current) return;
    lock.current = true;
    setView({ phase: "preparing", which });
    const path = which === "sponsor" ? "sponsor-contacts" : kind;
    const body = which === "sponsor" ? {} : exportBody(kind, applied);
    const result = await apiPost<Prepared>(`/api/admin/exports/${path}`, body);
    if (!result.ok) {
      return finish({ phase: "problem", problem: { code: result.error.code, retry: result.error.code === "UNAUTHENTICATED" ? "none" : "new_export" } });
    }
    job.current = { which, downloadUrl: result.data.download_url, statusUrl: result.data.status_url };
    setView({ phase: "waiting", rows: result.data.rows });
    startDownload(result.data.download_url);
    watch(result.data.status_url);
  }

  function retrySameLink() {
    if (!job.current || lock.current) return;
    lock.current = true;
    setView({ phase: "starting" });
    startDownload(job.current.downloadUrl);
    watch(job.current.statusUrl);
  }

  function checkAgain() {
    if (!job.current || lock.current) return;
    lock.current = true;
    setView({ phase: "starting" });
    watch(job.current.statusUrl);
  }

  const busy = BUSY_PHASES.has(view.phase);
  const preparing = view.phase === "preparing" ? view.which : null;
  const button = "min-h-11 rounded-xl border px-4 py-2 disabled:opacity-50";
  const retryButton = "min-h-11 w-fit rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-4 py-2";
  return <section aria-label={t("Export")} className="grid gap-3 rounded-xl border border-(--fm-line) bg-(--fm-tint) p-4">
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" className={`${button} border-(--fm-line-strong) bg-(--fm-tint)`} disabled={busy} onClick={() => void run("list")}>
        {preparing === "list" ? t("Preparing export…") : t("Export CSV")}
      </button>
      {kind === "attendees" && <button type="button" className={`${button} border-(--fm-warn-line) bg-(--fm-warn-soft) text-(--fm-warn)`} disabled={busy} onClick={() => void run("sponsor")}>
        {preparing === "sponsor" ? t("Preparing export…") : t("Export sponsor contacts")}
      </button>}
    </div>
    <p className="text-xs text-(--fm-muted)">{t("Exports every record that matches the applied filters, not only this page.")}</p>
    {pending && <p className="text-xs text-(--fm-warn)">{t("You changed the filters but did not press Search. The export uses the filters that are applied.")}</p>}
    {kind === "attendees" && <p className="text-xs text-(--fm-muted)">{t("Sponsor contacts: only active attendees who currently agree to share their details with sponsors. Includes phone and LinkedIn. The filters do not apply.")}</p>}

    {view.phase === "waiting" && <p role="status" className="text-sm text-(--fm-ink)">{`${t("Export prepared")} (${t("about")} ${view.rows} ${t("rows")}). ${t("Waiting for the download to start…")}`}</p>}
    {view.phase === "starting" && <p role="status" className="text-sm text-(--fm-ink)">{t("The server is preparing the file…")}</p>}
    {view.phase === "streaming" && <p role="status" className="text-sm text-(--fm-ink)">
      {view.written !== null && view.expected !== null ? `${t("The server is sending the file")}: ${view.written} ${t("of")} ${view.expected} ${t("rows")}…` : `${t("The server is sending the file")}…`}
    </p>}
    {view.phase === "finished" && <p role="status" className="text-sm text-(--fm-ok)">
      {`${t("The server finished sending the file")}${view.rows === null ? "" : view.rows === 0 ? ` (${t("header row only")})` : ` (${view.rows} ${t("rows")})`}.`}{" "}
      {t("This page cannot tell whether your browser saved it. Check your downloads.")}
    </p>}
    {view.phase === "problem" && <div className="grid gap-2">
      <p role="alert" className="text-sm text-(--fm-warn)">
        {t(errorText(view.problem.code))}
        {view.problem.written != null && view.problem.expected != null ? ` (${view.problem.written} ${t("of")} ${view.problem.expected} ${t("rows")} ${t("were sent")}.)` : ""}
      </p>
      {view.problem.retry === "same_link" && <button type="button" className={retryButton} onClick={retrySameLink}>{t("Try the download again")}</button>}
      {view.problem.retry === "new_export" && <button type="button" className={retryButton} onClick={() => void run(job.current?.which ?? "list")}>{t("Export again")}</button>}
      {view.problem.retry === "check_status" && <button type="button" className={retryButton} onClick={checkAgain}>{t("Check again")}</button>}
    </div>}
  </section>;
}
