"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { fetchMyCard, isCardUrl, type OwnCard } from "@/lib/card";
import { messageFor } from "@/lib/errors";
import { qrMatrix, qrPath } from "@/lib/qr";

import { CardView } from "./CardView";
import { QrCode } from "./QrCode";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "ok"; card: OwnCard }
  | { kind: "error"; code: string };

function stateFrom(result: ApiResult<OwnCard>): State {
  if (!result.ok) {
    const { code } = result.error;
    if (code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED") {
      window.localStorage.removeItem("facemark.offlineQr.v1");
      return { kind: "signed-out" };
    }
    return { kind: "error", code };
  }
  // Never encode anything but a card URL (§9.2), whatever came back.
  return isCardUrl(result.data.card_url)
    ? { kind: "ok", card: result.data }
    : { kind: "error", code: "INTERNAL_ERROR" };
}

type OfflineQrStore = {
  save: (pin: string, drawing: { dim: number; path: string }) => Promise<void>;
  hasCopy: () => boolean;
  clear: () => void;
};

declare global {
  interface Window { FacemarkOfflineQr?: OfflineQrStore }
}

function loadOfflineQrStore(): Promise<OfflineQrStore> {
  if (window.FacemarkOfflineQr) return Promise.resolve(window.FacemarkOfflineQr);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/offline-qr.js";
    script.onload = () => window.FacemarkOfflineQr ? resolve(window.FacemarkOfflineQr) : reject(new Error("Offline QR unavailable"));
    script.onerror = () => reject(new Error("Offline QR unavailable"));
    document.head.appendChild(script);
  });
}

function OfflineQrControls({ cardUrl }: { cardUrl: string }) {
  const { t } = useLanguage();
  const [pin, setPin] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setSaved((current) => current || window.localStorage.getItem("facemark.offlineQr.v1") !== null), 0);
    return () => window.clearTimeout(timer);
  }, []);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(false);
    if (!/^\d{6,12}$/.test(pin) || pin !== confirmation) {
      setError(true);
      return;
    }
    setBusy(true);
    try {
      const matrix = qrMatrix(cardUrl);
      const store = await loadOfflineQrStore();
      await store.save(pin, { dim: matrix.size + 8, path: qrPath(matrix, 4) });
      setSaved(true);
      setPin("");
      setConfirmation("");
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  function remove() {
    window.localStorage.removeItem("facemark.offlineQr.v1");
    setSaved(false);
  }

  return (
    <details className="fm-offline">
      <summary>{t("Offline QR")}</summary>
      <div className="fm-offline-body">
        <p className="fm-small">{t("Save an encrypted copy on this device. Open this app once while online, then use your PIN to show the QR when offline.")}</p>
        <p className="fm-alert fm-alert-warn">{t("The QR can be shown offline, but scanning it still needs a connection to the AI Summit server.")}</p>
        {saved && <p role="status" className="fm-alert fm-alert-ok">{t("Offline QR saved on this device.")}</p>}
        <form onSubmit={save} className="fm-form">
          <div className="fm-field">
            <label htmlFor="offline-pin" className="fm-label">{t("Choose a 6–12 digit PIN")}</label>
            <input id="offline-pin" type="password" inputMode="numeric" autoComplete="off" pattern="[0-9]{6,12}" minLength={6} maxLength={12} required value={pin} onChange={(event) => setPin(event.target.value)} className="fm-input" />
          </div>
          <div className="fm-field">
            <label htmlFor="offline-pin-again" className="fm-label">{t("Repeat PIN")}</label>
            <input id="offline-pin-again" type="password" inputMode="numeric" autoComplete="off" pattern="[0-9]{6,12}" minLength={6} maxLength={12} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} className="fm-input" />
          </div>
          {error && <p role="alert" className="fm-alert fm-alert-error">{t("Could not save offline QR. Check the PIN and try again.")}</p>}
          <button type="submit" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block">{busy ? t("Saving…") : t("Save offline QR")}</button>
        </form>
        {saved && <button type="button" onClick={remove} className="fm-btn fm-btn-secondary fm-btn-block">{t("Remove offline copy")}</button>}
      </div>
    </details>
  );
}
// The attendee's own QR. Which card this is comes only from the session
// cookie, resolved on the server — nothing here names an attendee.
export function MyCard() {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchMyCard().then((result) => {
      if (!cancelled) setState(stateFrom(result));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") {
    return <p role="status" className="fm-empty">{t("Preparing your networking QR…")}</p>;
  }
  if (state.kind === "signed-out") {
    return (
      <div className="fm-stack">
        <p className="fm-note">{t("Sign in to show your networking card.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">
          {t("Sign in")}
        </Link>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { card_url, card } = state.card;
  return (
    <div className="fm-stack-lg">
      <section aria-labelledby="qr-heading" className="fm-qr-stage">
        <div className="fm-pass-identity"><span className="fm-pass-label">AI Summit</span><strong>{card.first_name} {card.last_name}</strong><span>{card.profession}</span></div>
        <h2 id="qr-heading" className="sr-only">
          {t("Your QR code")}
        </h2>
        <div className="fm-qr-frame">
          <QrCode value={card_url} label={`QR code for ${card.first_name} ${card.last_name}'s networking card`} />
        </div>
        <p className="fm-qr-note">
          {t("Hold this code steady for networking. Attendees can open your card; booth staff can scan it for activities.")}
        </p>
      </section>

      <OfflineQrControls cardUrl={card_url} />

      <section className="fm-section">
        <h2 className="fm-eyebrow">{t("What people see when they scan it")}</h2>
        <CardView card={card} heading="h2" />
      </section>

      <AttendeeTabBar active="qr" />
    </div>
  );
}
