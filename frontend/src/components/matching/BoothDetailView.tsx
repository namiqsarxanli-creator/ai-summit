"use client";

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
    return <p className="text-sm text-slate-500">Loading booth…</p>;
  }

  if (state.kind === "error") {
    return (
      <p role="alert" className="text-sm text-amber-300">
        {messageFor(state.code)}
      </p>
    );
  }

  const { booth } = state;
  return (
    <article className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-50">{booth.name}</h1>
        {booth.location_label && <p className="text-sm text-slate-500">{booth.location_label}</p>}
      </header>

      {booth.description && <p className="text-sm text-slate-300">{booth.description}</p>}

      <section className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
          Things to try at this booth
        </h2>
        {booth.tasks.length === 0 ? (
          <p className="text-sm text-slate-500">No activities are listed for this booth yet.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {booth.tasks.map((task) => (
              <li
                key={task.title}
                className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"
              >
                <p className="font-medium text-slate-100">{task.title}</p>
                {task.description && (
                  <p className="mt-1 text-sm text-slate-400">{task.description}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <FeedbackForm boothSlug={booth.slug} />
    </article>
  );
}
