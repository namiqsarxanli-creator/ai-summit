"use client";

import { useEffect, useState } from "react";

import type { ApiError } from "@/lib/api";
import { fetchPublicCard, type PublicCard } from "@/lib/card";
import { messageFor } from "@/lib/errors";

import { CardView } from "./CardView";

type State =
  | { kind: "loading" }
  | { kind: "ok"; card: PublicCard }
  | { kind: "error"; error: ApiError };

// The page a scanned QR opens. No session is needed or consulted: the
// token in the URL is the whole capability (§9.2).
export function PublicCardView({ token }: { token: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchPublicCard(token).then((result) => {
      if (cancelled) return;
      setState(result.ok ? { kind: "ok", card: result.data } : { kind: "error", error: result.error });
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (state.kind === "loading") {
    return <p className="text-sm text-slate-500">Loading card…</p>;
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-5 text-amber-200">
        {messageFor(state.error.code)}
      </p>
    );
  }
  return <CardView card={state.card} />;
}
