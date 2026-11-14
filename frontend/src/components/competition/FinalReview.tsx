"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import {
  discardVersion,
  fetchVersion,
  fetchVersions,
  finalFailure,
  foundersCsvUrl,
  publishVersion,
  takeSnapshot,
  type FinalDetail,
  type Versions,
  type VersionSummary,
} from "@/lib/final";

import { OfflineReview } from "./OfflineReview";
import { confirmAction } from "@/lib/confirm";

type Notice = { tone: "ok" | "warn"; text: string } | null;
type State = { kind: "loading" } | { kind: "error"; text: string } | { kind: "ok"; data: Versions };

const STATUS_TEXT: Record<VersionSummary["status"], string> = {
  draft: "Draft · not shown yet",
  published: "Published",
  superseded: "Superseded by a newer version",
  discarded: "Discarded",
};

// The Game Master's review of the final result: take a snapshot after the close,
// read it, publish it for the Grand Final. A published version is never edited;
// a later correction needs a new version.
export function FinalReview() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [open, setOpen] = useState<FinalDetail | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const apply = useCallback((r: ApiResult<Versions>) => {
    setState(r.ok ? { kind: "ok", data: r.data } : { kind: "error", text: messageFor(r.error.code) });
  }, []);
  useEffect(() => {
    let active = true;
    void fetchVersions().then((r) => {
      if (active) apply(r);
    });
    return () => { active = false; };
  }, [apply]);

  async function run(action: () => Promise<ApiResult<VersionSummary>>, success: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const r = await action();
      setNotice(r.ok ? { tone: "ok", text: success } : { tone: "warn", text: finalFailure(r, messageFor) });
      apply(await fetchVersions());
      if (r.ok) {
        const detail = await fetchVersion(r.data.id);
        setOpen(detail.ok ? detail.data : null);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  async function review(id: number) {
    const detail = await fetchVersion(id);
    if (detail.ok) setOpen(detail.data);
    else setNotice({ tone: "warn", text: finalFailure(detail, messageFor) });
  }

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Checking your session…")}</p>;
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  return (
    <div className="fm-stack-lg">
      <section className="fm-card fm-section">
        <p className="fm-small">{t("The ranking closes at 17:30. Take a snapshot after the close, check it, then publish it for the Grand Final.")}</p>
        <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void run(takeSnapshot, "A new draft was taken.")}>
          {t("Take a snapshot of the ranking")}
        </button>
        {notice && <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{t(notice.text)}</p>}
      </section>

      {state.data.pending_offline_scans > 0 && (
        <p role="alert" className="fm-alert fm-alert-warn">{t("{n} late scans wait for review. Review them before you take the final snapshot.").replace("{n}", String(state.data.pending_offline_scans))}</p>
      )}
      <OfflineReview timezone={state.data.timezone} />

      <section className="fm-section" aria-labelledby="final-versions">
        <h2 id="final-versions" className="fm-eyebrow">{t("Versions")}</h2>
        {state.data.versions.length === 0 && <p className="fm-empty">{t("No snapshot yet.")}</p>}
        <ul className="fm-list">
          {state.data.versions.map((v) => (
            <li key={v.id} className="fm-card fm-section" data-testid={`version-${v.version}`}>
              <div className="fm-row-between">
                <p className="fm-strong">{t("Version {n}").replace("{n}", String(v.version))}</p>
                <span className="fm-badge">{t(STATUS_TEXT[v.status])}</span>
              </div>
              <p className="fm-small">{t("{n} people ranked · {f} Founders").replace("{n}", String(v.entries)).replace("{f}", String(v.founders))}</p>
              <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => void review(v.id)}>{t("Review")}</button>
              {v.status === "draft" && (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    className="fm-btn fm-btn-primary fm-btn-block"
                    onClick={() => { void confirmAction(t("Publish this version on the big screen and to every attendee?")).then((ok) => { if (ok) void run(() => publishVersion(v.id), "Published."); }); }}
                  >
                    {t("Publish")}
                  </button>
                  <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => void run(() => discardVersion(v.id), "Discarded.")}>{t("Discard")}</button>
                </>
              )}
              <a className="fm-btn fm-btn-quiet" href={foundersCsvUrl(v.id)}>{t("Lucky Draw list (CSV)")}</a>
            </li>
          ))}
        </ul>
      </section>

      {open && (
        <section className="fm-section" aria-labelledby="final-detail">
          <h2 id="final-detail" className="fm-eyebrow">{t("Version {n}").replace("{n}", String(open.version))}</h2>
          {open.ledger_changed_since && (
            <p role="alert" className="fm-alert fm-alert-warn">{t("Points changed after this snapshot (a correction or a verified offline scan). Take a new snapshot to include them.")}</p>
          )}
          <ol className="fm-list">
            {open.ranking.slice(0, 20).map((r) => (
              <li key={r.user_id} className="fm-row-between fm-small">
                <span>{r.place}. {r.name}</span>
                <span className="fm-strong">{r.points}</span>
              </li>
            ))}
          </ol>
          <p className="fm-small">{t("Founders: {n}").replace("{n}", String(open.founder_list.length))}</p>
        </section>
      )}
    </div>
  );
}
