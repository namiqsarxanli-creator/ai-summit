"use client";

// Camera lifecycle for the M10 staff scanner (ARCHITECTURE.md §13.1
// step 2-3, §13.5, §24.21). Rear camera preferred where supported;
// permission/availability failures are reported as typed statuses so
// the caller can show recovery instructions and the manual-entry
// fallback (never the other way around).
//
// Decoding is deliberately a pure, separately-tested function
// (`decodeQrFromImageData`) — this hook's own job is only camera
// plumbing: acquire the stream, draw frames to an off-screen canvas,
// hand each frame to the decoder, and guarantee every track is stopped
// on unmount or when `active` goes false. Once a frame decodes, the
// loop stops on its own — the caller unmounts this hook (by flipping
// `active`) to actually leave the camera state, which is what prevents
// a second decode of the same frame from firing a second resolve.

import { useEffect, useRef, useState } from "react";

import { decodeQrFromImageData } from "@/lib/qrDecode";

export type CameraStatus =
  | { kind: "starting" }
  | { kind: "streaming" }
  | { kind: "denied" }
  | { kind: "unavailable" }
  | { kind: "error" };

function statusForError(error: unknown): CameraStatus {
  const name = error instanceof Error ? error.name : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") return { kind: "denied" };
  if (name === "NotFoundError" || name === "DevicesNotFoundError") return { kind: "unavailable" };
  return { kind: "error" };
}

export function useCameraScanner(active: boolean, onDetected: (text: string) => void) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [status, setStatus] = useState<CameraStatus>({ kind: "starting" });
  const [attempt, setAttempt] = useState(0);

  const onDetectedRef = useRef(onDetected);
  useEffect(() => {
    onDetectedRef.current = onDetected;
  }, [onDetected]);

  useEffect(() => {
    if (!active) return;

    let cancelled = false;
    let stream: MediaStream | null = null;
    let rafId: number | null = null;

    function tick() {
      const video = videoRef.current;
      const canvas = canvasRef.current;
      if (video && canvas && video.readyState >= video.HAVE_CURRENT_DATA) {
        canvas.width = video.videoWidth || canvas.width;
        canvas.height = video.videoHeight || canvas.height;
        const ctx = canvas.getContext("2d");
        if (ctx && canvas.width > 0 && canvas.height > 0) {
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          const frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const text = decodeQrFromImageData(frame.data, frame.width, frame.height);
          if (text) {
            onDetectedRef.current(text);
            return; // stop decoding; the caller takes it from here (rescan remounts us)
          }
        }
      }
      rafId = requestAnimationFrame(tick);
    }

    async function start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" } },
          audio: false,
        });
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => {});
        }
        if (cancelled) return;
        setStatus({ kind: "streaming" });
        rafId = requestAnimationFrame(tick);
      } catch (error) {
        if (!cancelled) setStatus(statusForError(error));
      }
    }

    start();

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, [active, attempt]);

  function restart() {
    setStatus({ kind: "starting" });
    setAttempt((n) => n + 1);
  }

  return { videoRef, canvasRef, status, restart };
}
