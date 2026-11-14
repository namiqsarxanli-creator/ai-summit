"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import {
  actionFor,
  cancelChoice,
  chooseMasterclass,
  eventDate,
  programLabel,
  eventTime,
  fetchMyChoices,
  fetchProgram,
  type Action,
  type Block,
  type Choice,
  type ChoiceResult,
  type MasterclassItem,
  type MyChoices,
  type Program,
} from "@/lib/masterclasses";
import { playgroundError } from "@/lib/playground";
import type { ApiResult } from "@/lib/api";

type ProgramState = { kind: "loading" } | { kind: "error"; code: string } | { kind: "ok"; program: Program };
type MineState = { kind: "loading" } | { kind: "signed-out" } | { kind: "ok"; choices: Choice[]; revisions: Record<string, string> } | { kind: "error" };
// Shown in the card that was acted on, so it is seen where the tap happened.
type Notice = { tone: "ok" | "warn"; text: string; itemId: number } | null;
// The attendee is about to give up a confirmed seat for a waitlist place: asked first.
type Pending = { block: Block; item: MasterclassItem; current: MasterclassItem | undefined; expectedRevision: string } | null;

const OUTCOME_TEXT: Record<ChoiceResult["outcome"], string> = {
  confirmed: "Your seat is confirmed.",
  waitlisted: "You are on the waitlist. You get the seat automatically if one frees up.",
  cancelled: "Your choice was cancelled.",
  unchanged: "Nothing changed: this is already your choice.",
};

function fill(text: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce((out, [key, value]) => out.replace(`{${key}}`, String(value)), text);
}

// The masterclass programme by block, with the attendee's own choices on top.
// The programme loads for anyone; choosing needs a session. Every change goes to
// the server, whose answer (and a fresh programme) is what the screen then shows.
export function MasterclassProgram() {
  const { t, language } = useLanguage();
  const [program, setProgram] = useState<ProgramState>({ kind: "loading" });
  const [mine, setMine] = useState<MineState>({ kind: "loading" });
  const [busyBlock, setBusyBlock] = useState<number | null>(null);
  const inFlight = useRef(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [promoted, setPromoted] = useState<number[]>([]);
  const previousChoices = useRef<Choice[]>([]);
  const mounted = useRef(true);
  const refreshingRef = useRef(false);
  const epoch = useRef(0);

  const apply = useCallback((p: ApiResult<Program>, m: ApiResult<MyChoices>) => {
    setProgram(p.ok ? { kind: "ok", program: p.data } : { kind: "error", code: p.error.code });
    if (m.ok) {
      const upgraded = m.data.choices.filter(c => c.status === "confirmed" && previousChoices.current.some(old => old.block_id === c.block_id && old.masterclass_id === c.masterclass_id && old.status === "waitlisted"));
      setPromoted(upgraded.map(c => c.block_id));
      previousChoices.current = m.data.choices;
      setMine({ kind: "ok", choices: m.data.choices, revisions: m.data.revisions ?? {} });
    }
    else if (m.error.code === "UNAUTHENTICATED" || m.error.code === "REGISTRATION_NOT_VERIFIED") setMine({ kind: "signed-out" });
    else setMine({ kind: "error" });
  }, []);
  const load = useCallback(async () => {
    if (inFlight.current || refreshingRef.current || !mounted.current) return;
    refreshingRef.current = true;
    const requestEpoch = ++epoch.current;
    setRefreshing(true);
    try {
      const [p, m] = await Promise.all([fetchProgram(), fetchMyChoices()]);
      if (mounted.current && requestEpoch === epoch.current && !inFlight.current) apply(p, m);
    } finally {
      refreshingRef.current = false;
      if (mounted.current) setRefreshing(false);
    }
  }, [apply]);

  const invalidateReads = useCallback(() => { epoch.current++; }, []);
  useEffect(() => {
    mounted.current = true;
    void Promise.resolve().then(load);
    const visible = () => { if (document.visibilityState === "visible") void load(); };
    document.addEventListener("visibilitychange", visible);
    return () => { mounted.current = false; invalidateReads(); document.removeEventListener("visibilitychange", visible); };
  }, [load, invalidateReads]);

  const hasWaitlist = program.kind === "ok" && mine.kind === "ok" && mine.choices.some(c => c.status === "waitlisted");
  useEffect(() => {
    if (!hasWaitlist) return;
    // Only while actively waiting. A failed refresh leaves this state, stopping
    // automatic retries; the visible refresh button remains available.
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 15000);
    return () => window.clearInterval(timer);
  }, [hasWaitlist, load]);

  async function send(block: Block, itemId: number, run: () => Promise<ApiResult<ChoiceResult>>) {
    if (inFlight.current) return; // a double tap sends one request
    inFlight.current = true;
    epoch.current++;
    setBusyBlock(block.id);
    setNotice(null);
    const result = await run();
    if (!mounted.current) { inFlight.current = false; return; }
    if (result.ok) {
      previousChoices.current = result.data.choices.choices;
      setMine({ kind: "ok", choices: result.data.choices.choices, revisions: result.data.choices.revisions ?? {} });
      setNotice({ tone: "ok", text: OUTCOME_TEXT[result.data.outcome], itemId });
    } else {
      const { code, details } = result.error;
      if (code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED") setMine({ kind: "signed-out" });
      if (code === "MASTERCLASS_FULL") {
        setNotice({
          tone: "warn",
          text: details.waitlist_open === true
            ? "The last seat was just taken. You can join the waitlist instead."
            : messageFor(code),
          itemId,
        });
      } else setNotice({ tone: "warn", text: code === "PLAYGROUND_CONFLICT" ? playgroundError(details.reason) : messageFor(code), itemId });
    }
    const [fresh, own] = await Promise.all([fetchProgram(), result.ok ? Promise.resolve(null) : fetchMyChoices()]);
    if (mounted.current) {
      if (fresh.ok) setProgram({ kind: "ok", program: fresh.data });
      if (own?.ok) { previousChoices.current = own.data.choices; setMine({ kind: "ok", choices: own.data.choices, revisions: own.data.revisions ?? {} }); }
    }
    inFlight.current = false;
    if (mounted.current) setBusyBlock(null);
  }

  function act(block: Block, item: MasterclassItem, action: Action, current: MasterclassItem | undefined) {
    const expectedRevision = mine.kind === "ok" ? mine.revisions[String(block.id)] ?? "0" : "0";
    if (action.kind === "change-to-waitlist" && action.givesUpSeat) {
      setPending({ block, item, current, expectedRevision });
      return;
    }
    if (action.kind === "cancel") {
      void send(block, item.id, () => cancelChoice(block.id, expectedRevision));
      return;
    }
    const joinWaitlist = action.kind === "waitlist" || action.kind === "change-to-waitlist";
    void send(block, item.id, () => chooseMasterclass(block.id, item.id, { joinWaitlist, expectedRevision }));
  }

  function confirmGiveUp() {
    if (!pending) return;
    const { block, item, expectedRevision } = pending;
    setPending(null);
    void send(block, item.id, () => chooseMasterclass(block.id, item.id, { joinWaitlist: true, giveUpConfirmedSeat: true, expectedRevision }));
  }

  if (program.kind === "loading") return <p role="status" className="fm-note">{t("Loading the masterclass programme…")}</p>;
  if (program.kind === "error") {
    return (
      <div className="fm-stack">
        <p role="alert" className="fm-alert fm-alert-warn">{t(messageFor(program.code))}</p>
        <button type="button" className="fm-btn fm-btn-secondary" onClick={() => { setProgram({ kind: "loading" }); void load(); }}>{t("Retry")}</button>
      </div>
    );
  }

  const events = program.program.events;
  const choices = mine.kind === "ok" ? mine.choices : [];
  const signedIn = mine.kind === "ok";
  const byId = new Map<number, MasterclassItem>();
  for (const event of events) for (const block of event.blocks) for (const item of block.masterclasses) byId.set(item.id, item);

  if (events.length === 0 || events.every((e) => e.blocks.length === 0 && e.unscheduled.length === 0)) {
    return (
      <div className="fm-stack">
        <p className="fm-empty">{t("The masterclass programme will be published soon.")}</p>
        {signedIn && <AttendeeTabBar />}
      </div>
    );
  }

  return (
    <div className="fm-stack-lg">
      <button type="button" className="fm-btn fm-btn-secondary" disabled={refreshing || busyBlock !== null} onClick={() => void load()}>{t(refreshing ? "Refreshing programme…" : "Refresh programme")}</button>
      {mine.kind === "signed-out" && (
        <div className="fm-stack">
          <p className="fm-alert fm-alert-info">{t("Sign in to choose your masterclasses.")}</p>
          <Link href="/login" className="fm-btn fm-btn-primary fm-btn-block">{t("Sign in")}</Link>
        </div>
      )}
      {mine.kind === "error" && <p role="alert" className="fm-alert fm-alert-warn">{t("Your choices could not be loaded. Reload the page to try again.")}</p>}

      {events.map((event) => {
        const time = (iso: string) => eventTime(iso, event.timezone, language);
        return (
          <div key={event.code} className="fm-stack-lg">
            <section className="fm-section" aria-label={t("Event")}>
              <h2 className="fm-h2">{event.title}</h2>
              <p className="fm-small">
                {eventDate(event.event_date, language)}
                {event.venue ? ` · ${event.venue}` : ""}
              </p>
              <p className="fm-small">{t("Choose one masterclass in each block. A reservation is not a check-in: attendance is recorded at the hall.")}</p>
            </section>

            {signedIn && (
              <section className="fm-card fm-card-tint fm-section" aria-labelledby={`mine-${event.code}`}>
                <h2 id={`mine-${event.code}`} className="fm-eyebrow">{t("My choices")}</h2>
                {event.blocks.some(b => promoted.includes(b.id)) && <p role="status" className="fm-alert fm-alert-info">{t("Your seat was confirmed from the waitlist.")}</p>}
                <dl className="fm-dl">
                  {event.blocks.map((block) => {
                    const choice = choices.find((c) => c.block_id === block.id);
                    const item = choice ? byId.get(choice.masterclass_id) : undefined;
                    return (
                      <div key={block.id}>
                        <dt>{programLabel(block.title, block.display_number, "block", language)} · {time(block.starts_at)}–{time(block.ends_at)}</dt>
                        <dd className="fm-stack" style={{ gap: 6 }}>
                          {choice && item ? (
                            <>
                              <span className="fm-strong">{item.title}</span>
                              <span className={choice.status === "confirmed" ? "fm-status fm-status-ok" : "fm-status fm-status-bad"}>
                                {choice.status === "confirmed"
                                  ? t("Seat confirmed")
                                  : fill(t("On the waitlist · place {n}"), { n: choice.waitlist_position ?? "–" })}
                              </span>
                            </>
                          ) : (
                            <span>{t("No choice yet")}</span>
                          )}
                          <span className="fm-micro">
                            {block.changes_open
                              ? fill(t("You can change this until {time}."), { time: time(block.change_deadline) })
                              : fill(t("Changes closed at {time}."), { time: time(block.change_deadline) })}
                          </span>
                        </dd>
                      </div>
                    );
                  })}
                </dl>
              </section>
            )}

            {event.blocks.map((block) => {
              const choice = choices.find((c) => c.block_id === block.id);
              const current = choice ? byId.get(choice.masterclass_id) : undefined;
              return (
                <section key={block.id} className="fm-section" aria-labelledby={`block-${block.id}`}>
                  <div className="fm-row-between">
                    <h2 id={`block-${block.id}`} className="fm-h3">{programLabel(block.title, block.display_number, "block", language)}</h2>
                    <span className="fm-badge">{time(block.starts_at)}–{time(block.ends_at)}</span>
                  </div>
                  <p className="fm-small">{t("Leaving or changing a choice is unavailable after the deadline.")}</p>
                  <p className="fm-micro">
                    {block.changes_open
                      ? fill(t("Choices can be changed until {time}."), { time: time(block.change_deadline) })
                      : fill(t("Changes closed at {time}."), { time: time(block.change_deadline) })}
                  </p>
                  {block.masterclasses.length === 0 && <p className="fm-empty">{t("Masterclasses for this block will be announced soon.")}</p>}
                  <ul className="fm-list">
                    {block.masterclasses.map((item) => {
                      const action = actionFor(block, item, choice);
                      const isMine = choice?.masterclass_id === item.id;
                      const asking = pending?.item.id === item.id && pending.block.id === block.id;
                      return (
                        <li key={item.id} className="fm-card fm-section" data-testid={`masterclass-${item.id}`}>
                          <h3 className="fm-h3">{item.title}</h3>
                          <p className="fm-small">
                            {[item.room ? programLabel(item.room, item.room_display_number, "room", language) : null, item.speaker].filter(Boolean).join(" · ") || t("Room and speaker to be announced")}
                          </p>
                          {item.description && <p className="fm-text-soft">{item.description}</p>}
                          <p className="fm-small">
                            {item.availability === "available"
                              ? fill(t("{n} of {total} seats left"), { n: item.seats_left, total: item.capacity })
                              : item.availability === "waitlist"
                                ? fill(t("Full · waitlist {n} of {total}"), { n: item.waitlist_count, total: item.waitlist_capacity })
                                : t("No places left")}
                          </p>
                          {isMine && choice && (
                            <span className={choice.status === "confirmed" ? "fm-status fm-status-ok" : "fm-status fm-status-bad"}>
                              {choice.status === "confirmed" ? t("Your seat is confirmed") : fill(t("You are on the waitlist · place {n}"), { n: choice.waitlist_position ?? "–" })}
                            </span>
                          )}
                          {signedIn && !asking && (
                            <ActionButton
                              action={action}
                              mine={choice}
                              busy={busyBlock === block.id}
                              disabled={busyBlock !== null}
                              onClick={() => act(block, item, action, current)}
                            />
                          )}
                          {notice?.itemId === item.id && (
                            <p role={notice.tone === "ok" ? "status" : "alert"} className={`fm-alert ${notice.tone === "ok" ? "fm-alert-ok" : "fm-alert-warn"}`}>
                              {t(notice.text)}
                            </p>
                          )}
                          {asking && (
                            <div role="alertdialog" aria-labelledby={`ask-${item.id}`} className="fm-alert fm-alert-warn fm-stack">
                              <p id={`ask-${item.id}`}>
                                {fill(t("You have a confirmed seat in “{current}”. Joining the waitlist for “{target}” gives that seat up, and you may not get a seat here."), {
                                  current: pending?.current?.title ?? "",
                                  target: item.title,
                                })}
                              </p>
                              <button type="button" className="fm-btn fm-btn-primary fm-btn-block" onClick={confirmGiveUp}>{t("Give up my seat and join the waitlist")}</button>
                              <button type="button" className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => setPending(null)}>{t("Keep my seat")}</button>
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              );
            })}

            {event.unscheduled.length > 0 && (
              <section className="fm-section" aria-labelledby={`later-${event.code}`}>
                <h2 id={`later-${event.code}`} className="fm-h3">{t("Time and room to be announced")}</h2>
                <ul className="fm-list">
                  {event.unscheduled.map((item) => (
                    <li key={item.id} className="fm-card fm-section">
                      <h3 className="fm-h3">{item.title}</h3>
                      {item.speaker && <p className="fm-small">{item.speaker}</p>}
                      {item.description && <p className="fm-text-soft">{item.description}</p>}
                      <p className="fm-micro">{t("Booking opens once its time and room are set.")}</p>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        );
      })}
      {signedIn && <AttendeeTabBar />}
    </div>
  );
}

const ACTION_LABEL: Record<Exclude<Action["kind"], "none">, string> = {
  choose: "Choose",
  waitlist: "Join the waitlist",
  change: "Change my choice to this",
  "change-to-waitlist": "Change and join the waitlist",
  cancel: "Cancel",
};

function ActionButton({ action, mine, busy, disabled, onClick }: {
  action: Action; mine: Choice | undefined; busy: boolean; disabled: boolean; onClick: () => void;
}) {
  const { t } = useLanguage();
  if (action.kind === "none") {
    // "full" is already said by the availability line above the button.
    return action.reason === "closed" ? <p className="fm-micro">{t("Changes are closed for this block.")}</p> : null;
  }
  const label = action.kind === "cancel" && mine?.status === "waitlisted" ? "Leave the waitlist" : ACTION_LABEL[action.kind];
  const primary = action.kind === "choose" || action.kind === "change";
  return (
    <button
      type="button"
      className={`fm-btn fm-btn-block ${primary ? "fm-btn-primary" : "fm-btn-secondary"}`}
      disabled={disabled}
      aria-busy={busy || undefined}
      onClick={onClick}
    >
      {busy ? t("Saving…") : t(label)}
    </button>
  );
}
