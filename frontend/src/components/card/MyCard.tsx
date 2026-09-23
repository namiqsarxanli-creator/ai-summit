"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import type { ApiResult } from "@/lib/api";
import { fetchMyCard, isCardUrl, type OwnCard } from "@/lib/card";
import { messageFor } from "@/lib/errors";

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
    return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED"
      ? { kind: "signed-out" }
      : { kind: "error", code };
  }
  // Never encode anything but a card URL (§9.2), whatever came back.
  return isCardUrl(result.data.card_url)
    ? { kind: "ok", card: result.data }
    : { kind: "error", code: "INTERNAL_ERROR" };
}

// The attendee's own QR. Which card this is comes only from the session
// cookie, resolved on the server — nothing here names an attendee.
export function MyCard() {
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
    return <p className="text-sm text-slate-500">Loading your card…</p>;
  }
  if (state.kind === "signed-out") {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-slate-400">Sign in to show your networking card.</p>
        <Link
          href="/login"
          className="w-full rounded-lg bg-indigo-500 px-4 py-3 text-center font-semibold text-white transition-colors hover:bg-indigo-400"
        >
          Sign in
        </Link>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="text-sm text-amber-300">
        {messageFor(state.code)}
      </p>
    );
  }

  const { card_url, card } = state.card;
  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="qr-heading" className="flex flex-col items-center gap-4">
        <h2 id="qr-heading" className="sr-only">
          Your QR code
        </h2>
        <div className="w-full max-w-xs rounded-2xl bg-white p-3 shadow-lg shadow-indigo-500/10">
          <QrCode value={card_url} label={`QR code for ${card.first_name} ${card.last_name}'s networking card`} />
        </div>
        <p className="max-w-xs text-center text-sm text-slate-400">
          Let other attendees scan this to open your card. Booth staff scan the same code.
        </p>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
          What people see when they scan it
        </h2>
        <CardView card={card} heading="h2" />
      </section>

      <AttendeeTabBar active="qr" />
    </div>
  );
}
