"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { messageFor } from "@/lib/errors";
import { submitFeedback, type FeedbackResponse } from "@/lib/feedback";
import { fetchSession } from "@/lib/session";

const RATINGS = [1, 2, 3, 4, 5];

type SessionCheck = { kind: "loading" } | { kind: "signed-out" } | { kind: "signed-in" };

type FormState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "success"; result: FeedbackResponse }
  | { kind: "error"; code: string };

// M12: feedback is rewarded once per booth, but submitting more is
// always allowed and stored (§7.11, §15) — this form never disables
// itself after a successful submission, only after the "submitting"
// state, and a repeat submission is shown as a normal success, never
// an error.
export function FeedbackForm({ boothSlug }: { boothSlug: string }) {
  const [session, setSession] = useState<SessionCheck>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchSession().then((result) => {
      if (cancelled) return;
      setSession({ kind: result.kind === "signed-in" ? "signed-in" : "signed-out" });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (session.kind === "loading") return null;

  if (session.kind === "signed-out") {
    return (
      <section className="flex flex-col gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
          Leave feedback
        </h2>
        <p className="text-sm text-slate-400">
          <Link href="/login" className="font-medium text-indigo-300 hover:text-indigo-200">
            Sign in
          </Link>{" "}
          to leave feedback for this booth.
        </p>
      </section>
    );
  }

  return <FeedbackFormBody boothSlug={boothSlug} />;
}

function FeedbackFormBody({ boothSlug }: { boothSlug: string }) {
  const [rating, setRating] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [state, setState] = useState<FormState>({ kind: "idle" });
  const inFlight = useRef(false);

  function submit() {
    if (rating === null || inFlight.current) return;
    inFlight.current = true;
    setState({ kind: "submitting" });
    submitFeedback({
      booth_slug: boothSlug,
      rating,
      ...(comment.trim() ? { comment: comment.trim() } : {}),
    }).then((result) => {
      inFlight.current = false;
      if (!result.ok) {
        setState({ kind: "error", code: result.error.code });
        return;
      }
      setState({ kind: "success", result: result.data });
    });
  }

  function leaveMore() {
    setRating(null);
    setComment("");
    setState({ kind: "idle" });
  }

  return (
    <section className="flex flex-col gap-4 rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
        Leave feedback
      </h2>

      {state.kind === "success" && (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-emerald-300">
            {state.result.already_rewarded
              ? "Thanks for the feedback! You've already earned the reward at this booth."
              : `Thanks for the feedback! +${state.result.awarded_points} points awarded.`}
          </p>
          <p className="text-sm text-slate-400">New total: {state.result.total_points}</p>
          <button
            type="button"
            onClick={leaveMore}
            className="self-start rounded-lg border border-slate-700 px-4 py-2 text-sm font-semibold text-slate-200 transition-colors hover:bg-slate-800"
          >
            Leave more feedback
          </button>
        </div>
      )}

      {state.kind !== "success" && (
        <>
          <fieldset
            className="flex flex-col gap-2"
            disabled={state.kind === "submitting"}
          >
            <legend className="text-sm text-slate-400">How was your experience?</legend>
            <div role="radiogroup" aria-label="Rating" className="flex gap-2">
              {RATINGS.map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={rating === value}
                  onClick={() => setRating(value)}
                  className={`h-11 w-11 rounded-full border text-sm font-semibold transition-colors ${
                    rating === value
                      ? "border-indigo-400 bg-indigo-500 text-white"
                      : "border-slate-700 bg-slate-800 text-slate-300 hover:bg-slate-700"
                  }`}
                >
                  {value}
                </button>
              ))}
            </div>
          </fieldset>

          <label htmlFor="feedback-comment" className="text-sm text-slate-400">
            Comment (optional)
          </label>
          <textarea
            id="feedback-comment"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            disabled={state.kind === "submitting"}
            rows={3}
            className="rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-slate-100"
          />

          {state.kind === "error" && (
            <p role="alert" className="text-sm text-amber-300">
              {messageFor(state.code)}
            </p>
          )}

          <button
            type="button"
            onClick={submit}
            disabled={rating === null || state.kind === "submitting"}
            className="rounded-lg bg-indigo-500 px-4 py-3 font-semibold text-white transition-colors hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {state.kind === "submitting" ? "Submitting…" : "Submit feedback"}
          </button>
        </>
      )}
    </section>
  );
}
