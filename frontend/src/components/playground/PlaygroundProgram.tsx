"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import type { ApiResult } from "@/lib/api";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";
import {
  HOLDING,
  RELEASED,
  SEATS_PER_SLOT,
  STATUS_LABEL,
  acceptPlayground,
  bookPlayground,
  cancelPlayground,
  failureText,
  fetchPlayground,
  inspectPlayground,
  invitationLink,
  invitePlayground,
  slotLabel,
  type InvitationPreview,
  type PlaygroundBooking,
  type PlaygroundProgram as Program,
  type PlaygroundSlot,
} from "@/lib/playground";

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "error"; text: string }
  | { kind: "ok"; program: Program };
// Shown in the card that was acted on (slotId), or above the list (null).
type Notice = { tone: "ok" | "warn"; text: string; slotId: number | null } | null;
type Invitation =
  | { kind: "none" }
  | { kind: "checking"; token: string }
  | { kind: "ready"; token: string; preview: InvitationPreview }
  | { kind: "unavailable"; text: string };

function fill(text: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce((out, [key, value]) => out.replace(`{${key}}`, String(value)), text);
}

function inviteFromHash(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.hash.slice(1)).get("invite");
}

function clearHash() {
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

// The attendee's Playground: their own bookings, every participating booth's
// 15-minute sessions, team invitations and the on-site queue. Each change is one
// server request carrying the revision this screen saw; the screen then shows the
// server's state again. A booking is a place, never attendance or points.
export function PlaygroundProgram() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [booth, setBooth] = useState("");
  const [link, setLink] = useState("");
  const [invitation, setInvitation] = useState<Invitation>({ kind: "none" });

  const apply = useCallback((result: ApiResult<Program>) => {
    if (result.ok) setState({ kind: "ok", program: result.data });
    else if (result.error.code === "UNAUTHENTICATED" || result.error.code === "REGISTRATION_NOT_VERIFIED")
      setState({ kind: "signed-out" });
    else setState({ kind: "error", text: messageFor(result.error.code) });
  }, []);
  const load = useCallback(() => fetchPlayground().then(apply), [apply]);

  useEffect(() => {
    let active = true;
    const refresh = () =>
      void fetchPlayground().then((result) => {
        if (active) apply(result);
      });
    refresh();
    // The invitation is read on load AND when the fragment changes (a link
    // opened while this page was already showing changes only the fragment).
    const checkInvitation = () => {
      const token = inviteFromHash();
      if (!token) return;
      void inspectPlayground(token).then((result) => {
        if (!active) return;
        setInvitation(
          result.ok
            ? { kind: "ready", token, preview: result.data }
            : { kind: "unavailable", text: failureText(result, messageFor) },
        );
      });
    };
    checkInvitation();
    // Places move while the page is open: coming back shows the server's state.
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("hashchange", checkInvitation);
    return () => {
      active = false;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("hashchange", checkInvitation);
    };
  }, [apply]);

  async function run(
    slotId: number | null,
    action: () => Promise<ApiResult<unknown>>,
    success: string,
    done?: (data: unknown) => void,
  ) {
    if (inFlight.current) return; // a double tap sends one request
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const result = await action();
      if (result.ok) {
        setNotice({ tone: "ok", text: success, slotId });
        done?.(result.data);
      } else {
        // Never retried automatically: the attendee sees the refreshed state first.
        setNotice({ tone: "warn", text: failureText(result, messageFor), slotId });
      }
      await load();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const reserve = (slot: PlaygroundSlot) =>
    void run(slot.id, () => bookPlayground(slot, crypto.randomUUID()), "Your place is reserved.");
  const queue = (slot: PlaygroundSlot) =>
    void run(slot.id, () => bookPlayground(slot, crypto.randomUUID(), true), "You joined the on-site queue.");
  const cancel = (slot: PlaygroundSlot, booking: PlaygroundBooking, text = "Your place was cancelled.") =>
    void run(slot.id, () => cancelPlayground(booking), text);
  const invite = (slot: PlaygroundSlot) =>
    void run(slot.id, () => invitePlayground(slot, crypto.randomUUID()), "Invitation link created.", (data) => {
      setLink(invitationLink(window.location.origin, (data as { token: string }).token));
    });
  const accept = (token: string) =>
    void run(null, () => acceptPlayground(token), "Invitation accepted: your place is reserved.", () => {
      setInvitation({ kind: "none" });
      clearHash();
    });

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading Playground…")}</p>;
  if (state.kind === "signed-out") {
    return (
      <section className="fm-stack">
        <p className="fm-alert fm-alert-info">{t("Sign in to reserve a Playground session.")}</p>
        <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
      </section>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="fm-stack">
        <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>
        <button type="button" className="fm-btn fm-btn-secondary" onClick={() => { setState({ kind: "loading" }); void load(); }}>
          {t("Retry")}
        </button>
      </div>
    );
  }

  const { slots } = state.program;
  const booths = [...new Map(slots.map((s) => [s.booth_id, s.booth])).entries()];
  const chosen = booth || String(booths[0]?.[0] ?? "");
  const mine = slots.filter((s) => s.mine && (HOLDING.includes(s.mine.status) || s.mine.status === "no_show"));
  const time = (s: { starts_at: string; ends_at: string }, zone: string) =>
    `${eventTime(s.starts_at, zone, language)}–${eventTime(s.ends_at, zone, language)}`;
  const previewSlot = invitation.kind === "ready" ? slots.find((s) => s.id === invitation.preview.slot_id) : undefined;

  const card = (slot: PlaygroundSlot, place: "mine" | "slot") => (
    <SlotCard
      key={slot.id}
      testId={`pg-${place}-${slot.id}`}
      slot={slot}
      busy={busy}
      notice={notice?.slotId === slot.id ? notice : null}
      time={time(slot, slot.timezone)}
      onReserve={() => reserve(slot)}
      onQueue={() => queue(slot)}
      onCancel={(b) => cancel(slot, b)}
      onInvite={() => invite(slot)}
      onShowLink={(token) => setLink(invitationLink(window.location.origin, token))}
      onWithdraw={(b) => cancel(slot, b, "The invitation was withdrawn.")}
    />
  );

  return (
    <div className="fm-stack-lg">
      <section className="fm-section" aria-label={t("AI Playground")}>
        <p className="fm-small">{t("Up to 3 active bookings. Each teammate accepts their own place. A booking is not attendance.")}</p>
        <p className="fm-small">{t("Cancel at least 15 minutes before. Arrive early: after 3 minutes your place goes to the queue.")}</p>
        <button type="button" disabled={busy} className="fm-btn fm-btn-secondary" onClick={() => void load()}>
          {t("Refresh programme")}
        </button>
      </section>

      {notice?.slotId === null && (
        <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
          {t(notice.text)}
        </p>
      )}

      {invitation.kind === "ready" && (
        <section className="fm-card fm-card-tint fm-section" aria-labelledby="pg-invitation">
          <h2 id="pg-invitation" className="fm-h3">{t("Team invitation")}</h2>
          <p className="fm-text-soft">
            {previewSlot ? `${t(previewSlot.booth)} · ${slotLabel(previewSlot)} · ${time(invitation.preview, previewSlot.timezone)}` : null}
          </p>
          {previewSlot && (
            <p className="fm-micro">
              {fill(t("Accept before {time} to keep the place."), { time: eventTime(invitation.preview.expires_at, previewSlot.timezone, language) })}
            </p>
          )}
          <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={() => accept(invitation.token)}>
            {t("Accept invitation")}
          </button>
          <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => { setInvitation({ kind: "none" }); clearHash(); }}>
            {t("Not now")}
          </button>
        </section>
      )}
      {invitation.kind === "unavailable" && <p role="alert" className="fm-alert fm-alert-warn">{t(invitation.text)}</p>}

      {link && (
        <section className="fm-card fm-section" aria-labelledby="pg-share">
          <h2 id="pg-share" className="fm-h3">{t("Share with a registered attendee")}</h2>
          <label className="fm-field">
            <span className="fm-label">{t("Invitation link")}</span>
            <input className="fm-input" readOnly value={link} onFocus={(e) => e.currentTarget.select()} />
          </label>
          <p className="fm-micro">{t("The link expires after 30 minutes or 15 minutes before the session, whichever comes first.")}</p>
          <button
            type="button"
            className="fm-btn fm-btn-secondary fm-btn-block"
            onClick={() =>
              void navigator.clipboard
                .writeText(link)
                .then(() => setNotice({ tone: "ok", text: "Link copied.", slotId: null }))
                .catch(() => setNotice({ tone: "warn", text: "Select and copy the invitation link.", slotId: null }))
            }
          >
            {t("Copy link")}
          </button>
          <button type="button" className="fm-btn fm-btn-quiet" onClick={() => setLink("")}>{t("Close")}</button>
        </section>
      )}

      <section className="fm-section" aria-labelledby="pg-mine">
        <h2 id="pg-mine" className="fm-eyebrow">{t("My Playground bookings")}</h2>
        {mine.length > 0 ? <ul className="fm-list">{mine.map((s) => card(s, "mine"))}</ul> : <p className="fm-empty">{t("No Playground bookings yet.")}</p>}
      </section>

      {slots.length === 0 ? (
        <p className="fm-empty">{t("The organiser has not opened Playground bookings yet.")}</p>
      ) : (
        <section className="fm-section" aria-labelledby="pg-slots">
          <h2 id="pg-slots" className="fm-eyebrow">{t("Playground slots")}</h2>
          <label className="fm-field">
            <span className="fm-label">{t("Booth")}</span>
            <select className="fm-input" value={chosen} onChange={(e) => setBooth(e.target.value)}>
              {booths.map(([id, name]) => <option key={id} value={id}>{t(name)}</option>)}
            </select>
          </label>
          {(() => {
            const about = slots.find((s) => String(s.booth_id) === chosen)?.booth_description;
            return about ? (
              <div className="fm-booth-about" data-testid="booth-about">
                <p className="fm-eyebrow">{t("What awaits you at this booth")}</p>
                <p className="fm-booth-about-text">{t(about)}</p>
              </div>
            ) : null;
          })()}
          <ul className="fm-list">
            {/* Ended sessions (the server closed even the queue) leave the list. */}
            {slots.filter((s) => String(s.booth_id) === chosen && s.queue_open).map((s) => card(s, "slot"))}
          </ul>
        </section>
      )}
      <AttendeeTabBar />
    </div>
  );
}

function SlotCard({
  testId, slot, busy, notice, time, onReserve, onQueue, onCancel, onInvite, onShowLink, onWithdraw,
}: {
  testId: string;
  slot: PlaygroundSlot;
  busy: boolean;
  notice: Notice;
  time: string;
  onReserve: () => void;
  onQueue: () => void;
  onCancel: (b: PlaygroundBooking) => void;
  onInvite: () => void;
  onShowLink: (token: string) => void;
  onWithdraw: (b: PlaygroundBooking) => void;
}) {
  const { t } = useLanguage();
  const mine = slot.mine;
  const free = !mine || RELEASED.includes(mine.status);
  const walkInOnly = slot.number > 34;
  return (
    <li className="fm-card fm-section" data-testid={testId}>
      <div className="fm-row-between">
        <h3 className="fm-h3">{t(slot.booth)}</h3>
        <span className="fm-badge">{slotLabel(slot)}</span>
      </div>
      <p className="fm-small">{time}</p>
      <p className="fm-small">
        {walkInOnly
          ? t("Walk-in only · no advance booking")
          : fill(t("{n} of {total} places free"), { n: slot.seats_left, total: SEATS_PER_SLOT })}
      </p>
      {mine && (
        <p className={mine.status === "confirmed" || mine.status === "checked_in" ? "fm-status fm-status-ok" : "fm-status"}>
          {t(STATUS_LABEL[mine.status] ?? mine.status)}
          {mine.status === "waitlisted" && slot.queue_position ? ` · ${fill(t("place {n}"), { n: slot.queue_position })}` : ""}
        </p>
      )}
      {mine?.status === "no_show" && <p className="fm-micro">{t("You were more than 3 minutes late, so the place went to the queue. You can join the on-site queue.")}</p>}
      {free && slot.bookable && slot.seats_left > 0 && (
        <button type="button" disabled={busy} className="fm-btn fm-btn-primary fm-btn-block" onClick={onReserve}>{t("Reserve 15 minutes")}</button>
      )}
      {free && slot.queue_open && !(slot.bookable && slot.seats_left > 0) && (
        <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={onQueue}>{t("Join the on-site queue")}</button>
      )}
      {mine?.status === "confirmed" && slot.bookable && slot.seats_left > 0 && (
        <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={onInvite}>{t("Invite a teammate")}</button>
      )}
      {slot.cancellable && mine && (
        <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => onCancel(mine)}>
          {t(mine.status === "waitlisted" ? "Leave the queue" : "Cancel my place")}
        </button>
      )}
      {slot.invitations.map((hold) => (
        <div key={hold.id} className="fm-alert fm-alert-info fm-stack">
          <span>{t("Invitation hold")}</span>
          <button type="button" disabled={busy} className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => onShowLink(hold.token)}>{t("Show invitation link")}</button>
          <button type="button" disabled={busy} className="fm-btn fm-btn-quiet" onClick={() => onWithdraw(hold)}>{t("Withdraw invitation")}</button>
        </div>
      ))}
      {notice && (
        <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
          {t(notice.text)}
        </p>
      )}
    </li>
  );
}
