"use client";

import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { Avatar } from "@/components/ui/Avatar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import type { PhotoUrls } from "@/lib/avatar";
import { messageFor } from "@/lib/errors";
import { photoProblem, removePhoto, uploadPhoto } from "@/lib/profile";

type Status =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "removing" }
  | { kind: "done"; text: string }
  | { kind: "error"; text: string };

// The attendee's profile photo: choose, preview, upload, remove.
//
// Nothing is uploaded until "Upload photo" is pressed; the chosen picture is
// shown as a local preview first (a blob: URL, never sent anywhere), so the person
// sees what the circle will look like. An error never discards the chosen
// picture - the person can retry or pick another. The server re-checks
// everything (content, size, pixels); the checks here only spare a pointless
// upload.
export function PhotoControl({
  photo,
  onChanged,
  onSignedOut,
}: {
  photo: PhotoUrls | null;
  onChanged: (photo: PhotoUrls | null) => void;
  onSignedOut: () => void;
}) {
  const { t } = useLanguage();
  const input = useRef<HTMLInputElement>(null);
  // The chosen picture and the local URL its preview is shown from. The URL is made
  // when the picture is chosen and released when it is replaced, discarded,
  // uploaded or the screen goes away.
  const [picked, setPicked] = useState<{ file: File; url: string } | null>(null);
  const urlRef = useRef<string | null>(null);
  const [unreadable, setUnreadable] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const busy = status.kind === "uploading" || status.kind === "removing";
  const file = picked?.file ?? null;
  const preview = picked?.url ?? null;

  function release() {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
  }
  useEffect(() => release, []);

  function choose(next: File | null) {
    release();
    if (next) urlRef.current = URL.createObjectURL(next);
    setPicked(next && urlRef.current ? { file: next, url: urlRef.current } : null);
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.target.files?.[0] ?? null;
    event.target.value = ""; // choosing the same file again must fire a change
    setUnreadable(false);
    if (!chosen) return;
    const problem = photoProblem(chosen);
    if (problem) {
      choose(null);
      setStatus({ kind: "error", text: problem });
      return;
    }
    setStatus({ kind: "idle" });
    choose(chosen);
  }

  function cancelPick() {
    choose(null);
    setUnreadable(false);
    setStatus({ kind: "idle" });
  }

  async function onUpload() {
    if (!file || busy) return;
    setStatus({ kind: "uploading" });
    const result = await uploadPhoto(file);
    if (result.ok) {
      choose(null);
      onChanged(result.data.photo);
      setStatus({ kind: "done", text: "Photo updated." });
      return;
    }
    if (result.error.code === "UNAUTHENTICATED") return onSignedOut();
    // The chosen picture stays, so the person can retry or choose another.
    setStatus({ kind: "error", text: messageFor(result.error.code) });
  }

  async function onRemove() {
    if (busy) return;
    setStatus({ kind: "removing" });
    const result = await removePhoto();
    if (result.ok) {
      choose(null);
      onChanged(null);
      setStatus({ kind: "done", text: "Photo removed." });
      return;
    }
    if (result.error.code === "UNAUTHENTICATED") return onSignedOut();
    setStatus({ kind: "error", text: messageFor(result.error.code) });
  }

  return (
    <section className="fm-photo-control" aria-busy={busy}>
      <h2 className="sr-only">{t("Profile photo")}</h2>
      <Avatar size="profile" url={photo?.large} previewUrl={file && !unreadable ? preview : null} />
      {file ? (
        <>
          <p className="fm-small">{t("Preview — not uploaded yet")}</p>
          <div className="fm-row" style={{ justifyContent: "center" }}>
            <button
              type="button"
              onClick={onUpload}
              disabled={busy || unreadable}
              className="fm-btn fm-btn-primary"
            >
              {status.kind === "uploading" ? t("Uploading…") : t("Upload photo")}
            </button>
            <button
              type="button"
              onClick={cancelPick}
              disabled={busy}
              className="fm-btn fm-btn-secondary"
            >
              {t("Cancel")}
            </button>
          </div>
        </>
      ) : (
        <div className="fm-row" style={{ justifyContent: "center" }}>
          <button
            type="button"
            onClick={() => input.current?.click()}
            disabled={busy}
            className="fm-btn fm-btn-secondary"
          >
            {photo ? t("Change photo") : t("Add a photo")}
          </button>
          {photo && (
            <button
              type="button"
              onClick={onRemove}
              disabled={busy}
              className="fm-btn fm-btn-secondary"
            >
              {status.kind === "removing" ? t("Removing…") : t("Remove photo")}
            </button>
          )}
        </div>
      )}

      <input
        ref={input}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        className="sr-only"
        aria-label={t("Choose a photo")}
        tabIndex={-1}
        onChange={onPick}
      />
      {file && preview && (
        // Decodes the chosen picture off-screen: if the browser cannot read it, say so
        // now instead of after an upload.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={preview} alt="" className="hidden" onError={() => setUnreadable(true)} />
      )}

      <p className="fm-small">
        {t("Your photo appears on the leaderboard and on your contact card, where other attendees can see it.")}
      </p>
      <p className="fm-small">
        {t("JPEG, PNG or WebP, up to 8 MB. It is cropped to a square, and location and camera details are removed.")}
      </p>

      {unreadable && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t("We can't read this picture. Choose another one.")}
        </p>
      )}
      {status.kind === "error" && (
        <p role="alert" className="fm-alert fm-alert-error">
          {t(status.text)}
        </p>
      )}
      {status.kind === "done" && (
        <p role="status" className="fm-status fm-status-ok">
          {t(status.text)}
        </p>
      )}
    </section>
  );
}
