"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import {
  answerPairPhoto,
  fetchPairPhotos,
  pairPhotoFailure,
  safePhotoUrl,
  screenConsent,
  uploadPairPhoto,
  withdrawPairPhoto,
  type PairPhoto,
} from "@/lib/pairPhotos";
import { fetchConnections, type ConnectionRow } from "@/lib/people";

type Notice = { tone: "ok" | "warn"; text: string } | null;

// Beyond Now Photo: share a photo with someone you are connected with. It is not
// your profile photo and earns no points. It appears on the big screen only if
// BOTH of you agree and a moderator approves it; either of you can withdraw it.
export function PairPhotos() {
  const { t } = useLanguage();
  const [photos, setPhotos] = useState<PairPhoto[] | null>(null);
  const [connections, setConnections] = useState<ConnectionRow[]>([]);
  const [partner, setPartner] = useState("");
  const [screen, setScreen] = useState(false);
  const [picked, setPicked] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const file = useRef<HTMLInputElement>(null);

  const apply = useCallback((p: ApiResult<{ photos: PairPhoto[] }>, c: ApiResult<{ connections: ConnectionRow[] }>) => {
    setPhotos(p.ok ? p.data.photos : null);
    if (c.ok) setConnections(c.data.connections);
  }, []);
  useEffect(() => {
    let active = true;
    void Promise.all([fetchPairPhotos(), fetchConnections()]).then(([p, c]) => {
      if (active) apply(p, c);
    });
    return () => { active = false; };
  }, [apply]);

  async function run(action: () => Promise<ApiResult<unknown>>, success: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const r = await action();
      setNotice(r.ok ? { tone: "ok", text: success } : { tone: "warn", text: pairPhotoFailure(r, messageFor) });
      const [p, c] = await Promise.all([fetchPairPhotos(), fetchConnections()]);
      apply(p, c);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (photos === null) return null; // signed out: the page around it explains
  return (
    <section className="fm-section" aria-labelledby="pair-photos">
      <h2 id="pair-photos" className="fm-eyebrow">{t("Beyond Now Photo")}</h2>
      <p className="fm-small">{t("A photo of the two of you in front of the checkered wall. It is not your profile photo and earns no points.")}</p>
      {connections.length === 0 ? (
        <p className="fm-empty">{t("Connect with someone first: scan each other's QR.")}</p>
      ) : (
        <div className="fm-card fm-section">
          <label className="fm-field">
            <span className="fm-label">{t("Who is in the photo with you")}</span>
            <select className="fm-input" value={partner} onChange={(e) => setPartner(e.target.value)}>
              <option value="">—</option>
              {connections.map((c) => <option key={c.user_id} value={c.user_id}>{c.name}</option>)}
            </select>
          </label>
          {/* The browser's own file control is not translated: a labelled button opens it. */}
          <input
            ref={file}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="sr-only"
            aria-label={t("Choose a photo")}
            tabIndex={-1}
            onChange={(e) => setPicked(e.target.files?.[0]?.name ?? "")}
          />
          <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => file.current?.click()}>
            {t("Choose a photo")}
          </button>
          {picked && <p className="fm-small" style={{ overflowWrap: "anywhere" }}>{picked}</p>}
          <label className="fm-choice">
            <input type="checkbox" checked={screen} onChange={(e) => setScreen(e.target.checked)} />
            {t("I agree that it may be shown on the main screen (the other person decides for themselves).")}
          </label>
          <button
            type="button"
            disabled={busy || !partner}
            className="fm-btn fm-btn-primary fm-btn-block"
            onClick={() => {
              const picked = file.current?.files?.[0];
              if (!picked) { setNotice({ tone: "warn", text: "Choose a photo first." }); return; }
              void run(() => uploadPairPhoto(Number(partner), screen, picked), "Shared. The other person can now accept it.");
            }}
          >
            {t("Share the photo")}
          </button>
        </div>
      )}
      {notice && <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>{t(notice.text)}</p>}
      {photos.length > 0 && (
        <ul className="fm-list">
          {photos.map((p) => {
            const src = safePhotoUrl(p.thumb_url);
            return (
              <li key={p.id} className="fm-card fm-section" data-testid={`pair-${p.id}`}>
                {/* eslint-disable-next-line @next/next/no-img-element -- a server-made, fixed-shape URL */}
                {src && <img src={src} alt={t("Photo with {name}").replace("{name}", p.with)} width={128} height={128} className="fm-avatar-img" style={{ position: "static", borderRadius: 12 }} />}
                <p className="fm-strong">{t("With {name}").replace("{name}", p.with)}</p>
                <p className="fm-micro">
                  {p.status === "pending"
                    ? t(p.role === "partner" ? "Waiting for your answer" : "Waiting for their answer")
                    : p.on_screen ? t("On the main screen") : t("Not on the main screen")}
                </p>
                {p.role === "partner" && p.status === "pending" && (
                  <>
                    <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void run(() => answerPairPhoto(p.id, true, false), "Accepted.")}>{t("Accept")}</button>
                    <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => void run(() => answerPairPhoto(p.id, false, false), "Declined. The photo was deleted.")}>{t("Decline")}</button>
                  </>
                )}
                {(p.status === "accepted" || p.role === "uploader") && (
                  <label className="fm-choice">
                    <input type="checkbox" disabled={busy} checked={p.my_screen_consent} onChange={(e) => void run(() => screenConsent(p.id, e.target.checked), "Saved.")} />
                    {t("May be shown on the main screen")}
                  </label>
                )}
                <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => { if (window.confirm(t("Withdraw and delete this photo for both of you?"))) void run(() => withdrawPairPhoto(p.id), "Withdrawn. The photo was deleted."); }}>{t("Withdraw the photo")}</button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
