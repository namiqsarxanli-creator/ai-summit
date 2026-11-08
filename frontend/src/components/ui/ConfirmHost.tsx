"use client";

import { useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { registerConfirmHost, type ConfirmRequest } from "@/lib/confirm";

// The site's confirmation dialog (lib/confirm.ts). One question at a time; a
// second one waits its turn. Escape or a click outside the card means "no".
export function ConfirmHost() {
  const { t } = useLanguage();
  const [queue, setQueue] = useState<ConfirmRequest[]>([]);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const current = queue[0];

  useEffect(() => registerConfirmHost((request) => setQueue((q) => [...q, request])), []);

  useEffect(() => {
    if (!current) return;
    confirmButton.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") answer(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function answer(ok: boolean) {
    if (!current) return;
    current.resolve(ok);
    setQueue((q) => q.slice(1));
  }

  if (!current) return null;
  return (
    <div className="fm-dialog-backdrop" onClick={(event) => { if (event.target === event.currentTarget) answer(false); }}>
      <div role="alertdialog" aria-modal="true" aria-labelledby="fm-dialog-text" className="fm-dialog fm-card">
        <p id="fm-dialog-text" className="fm-dialog-text">{current.message}</p>
        <div className="fm-dialog-actions">
          <button type="button" className="fm-btn fm-btn-secondary" onClick={() => answer(false)}>{t("Cancel")}</button>
          <button ref={confirmButton} type="button" className="fm-btn fm-btn-primary" onClick={() => answer(true)}>{t("Confirm")}</button>
        </div>
      </div>
    </div>
  );
}
