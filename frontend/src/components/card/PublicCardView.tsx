"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
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
  const { t } = useLanguage();
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
    return <p role="status" className="fm-note">{t("Loading card…")}</p>;
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.error.code))}
      </p>
    );
  }
  return <CardView card={state.card} />;
}
