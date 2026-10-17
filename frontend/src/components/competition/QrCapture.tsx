"use client";

import { useState } from "react";

import { useCameraScanner } from "@/components/staff/useCameraScanner";
import { useLanguage } from "@/components/ui/LanguageProvider";

// Camera QR capture with a typed fallback. It only READS: the caller decides what
// the text is and sends it. While `paused` (a request in flight) the camera is
// off, so one QR held in front of it cannot fire twice.
export function QrCapture({ paused, onText, manualLabel }: { paused: boolean; onText: (text: string) => void; manualLabel: string }) {
  const { t } = useLanguage();
  const [on, setOn] = useState(false);
  const [manual, setManual] = useState("");
  const camera = useCameraScanner(on && !paused, (text) => {
    setOn(false);
    onText(text);
  });
  const { status, videoRef, canvasRef } = camera;
  return (
    <div className="fm-stack">
      {on && !paused ? (
        <div className="fm-camera">
          <video ref={videoRef} autoPlay className={`fm-camera-video ${status.kind === "streaming" ? "" : "invisible"}`} muted playsInline aria-label={t("Camera")} />
          <canvas ref={canvasRef} hidden />
          {status.kind !== "streaming" && (
            <p className="fm-camera-msg fm-camera-center">
              {t(status.kind === "denied" ? "Camera permission was refused. Allow the camera in the browser settings, or type the code below."
                : status.kind === "unavailable" ? "No camera is available. Type the code below."
                : status.kind === "error" ? "The camera could not start. Type the code below."
                : "Starting the camera…")}
            </p>
          )}
        </div>
      ) : (
        <button type="button" disabled={paused} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => setOn(true)}>
          {t("Scan with the camera")}
        </button>
      )}
      {on && <button type="button" className="fm-btn fm-btn-quiet" onClick={() => setOn(false)}>{t("Stop the camera")}</button>}
      <form
        className="fm-stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (manual.trim()) onText(manual.trim());
          setManual("");
        }}
      >
        <label className="fm-field">
          <span className="fm-label">{t(manualLabel)}</span>
          <input className="fm-input" value={manual} onChange={(e) => setManual(e.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} />
        </label>
        <button type="submit" disabled={paused || !manual.trim()} className="fm-btn fm-btn-secondary fm-btn-block">{t("Send")}</button>
      </form>
    </div>
  );
}
