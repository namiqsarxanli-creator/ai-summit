"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
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
  const { t } = useLanguage();
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
      <section className="fm-card fm-section">
        <h2 className="fm-eyebrow">{t("Leave feedback")}</h2>
        <p className="fm-text-soft">
          <Link href="/login" className="fm-link">
            {t("Sign in")}
          </Link>{" "}
          {t("to leave feedback for this booth.")}
        </p>
      </section>
    );
  }

  return <FeedbackFormBody boothSlug={boothSlug} />;
}

function FeedbackFormBody({ boothSlug }: { boothSlug: string }) {
  const { t } = useLanguage();
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
    <section className="fm-card fm-section">
      <h2 className="fm-eyebrow">{t("Leave feedback")}</h2>

      {state.kind === "success" && (
        <div className="fm-stack">
          <p role="status" className="fm-alert fm-alert-ok">
            {state.result.already_rewarded
              ? t("Thanks for the feedback! You've already earned the reward at this booth.")
              : `${t("Thanks for the feedback!")} +${state.result.awarded_points} ${t("points awarded.")}`}
          </p>
          <p className="fm-small">{t("New total:")} {state.result.total_points}</p>
          <button
            type="button"
            onClick={leaveMore}
            className="fm-btn fm-btn-secondary fm-btn-block"
          >
            {t("Leave more feedback")}
          </button>
        </div>
      )}

      {state.kind !== "success" && (
        <>
          <fieldset className="fm-fieldset" disabled={state.kind === "submitting"}>
            <legend className="fm-legend">{t("How was your experience?")}</legend>
            <div role="radiogroup" aria-label={t("Rating")} className="fm-rating-group">
              {RATINGS.map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={rating === value}
                  onClick={() => setRating(value)}
                  className="fm-rating"
                >
                  {value}
                </button>
              ))}
            </div>
          </fieldset>

          <label htmlFor="feedback-comment" className="fm-label">
            {t("Comment (optional)")}
          </label>
          <textarea
            id="feedback-comment"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            disabled={state.kind === "submitting"}
            rows={3}
            className="fm-input"
          />

          {state.kind === "error" && (
            <p role="alert" className="fm-alert fm-alert-warn">
              {t(messageFor(state.code))}
            </p>
          )}

          <button
            type="button"
            onClick={submit}
            disabled={rating === null || state.kind === "submitting"}
            className="fm-btn fm-btn-primary fm-btn-block"
          >
            {state.kind === "submitting" ? t("Submitting…") : t("Submit feedback")}
          </button>
        </>
      )}
    </section>
  );
}
