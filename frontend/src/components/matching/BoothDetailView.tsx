"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { useEffect, useState } from "react";

import { messageFor } from "@/lib/errors";
import { fetchBoothDetail, type BoothDetail } from "@/lib/matching";

import { FeedbackForm } from "./FeedbackForm";

type State =
  | { kind: "loading" }
  | { kind: "ok"; booth: BoothDetail }
  | { kind: "error"; code: string };

// M08's booth detail screen: "tasks and location". Unauthenticated,
// like `GET /api/booths/{slug}` itself (§10) — the public catalogue,
// not a personalized view, so this needs no session.
export function BoothDetailView({ slug }: { slug: string }) {
  const { t } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchBoothDetail(slug).then((result) => {
      if (cancelled) return;
      setState(
        result.ok ? { kind: "ok", booth: result.data } : { kind: "error", code: result.error.code },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  if (state.kind === "loading") {
    return <p role="status" className="fm-note">{t("Loading booth…")}</p>;
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="fm-alert fm-alert-warn">
        {t(messageFor(state.code))}
      </p>
    );
  }

  const { booth } = state;
  return (
    <article className="fm-stack-lg">
      <header className="fm-booth-hero fm-stack" style={{ gap: 4 }}>
        <h1>{t(booth.name)}</h1>
        {booth.location_label && <p className="fm-small">{t(booth.location_label)}</p>}
      </header>

      {booth.description && <p className="fm-text-soft">{t(booth.description)}</p>}

      <section className="fm-section">
        <h2 className="fm-eyebrow">{t("Things to try at this booth")}</h2>
        {booth.tasks.length === 0 ? (
          <p className="fm-empty">{t("No activities are listed for this booth yet.")}</p>
        ) : (
          <ul className="fm-list">
            {booth.tasks.map((task) => (
              <li key={t(task.title)} className="fm-activity">
                <p className="fm-strong">{t(task.title)}</p>
                {task.description && <p className="fm-small">{t(task.description)}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <FeedbackForm boothSlug={booth.slug} />
    </article>
  );
}
