"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { messageFor } from "@/lib/errors";
import { eventDate, eventTime } from "@/lib/masterclasses";
import {
  describe, fetchNotifications, lastSeen, markSeen, unreadCount,
  type Notification, type NotificationFeed,
} from "@/lib/notifications";

const REFRESH_MS = 120_000;

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "error"; text: string }
  | { kind: "ok"; feed: NotificationFeed; seen: number };

// The bell on Home: how many notifications arrived since this device last opened
// the list. Refreshed every two minutes while the page is in view.
export function NotificationBell() {
  const { t } = useLanguage();
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    const load = () => {
      if (document.visibilityState !== "visible") return;
      void fetchNotifications().then((r) => {
        if (active && r.ok && Array.isArray(r.data.items)) setCount(unreadCount(r.data.items, lastSeen()));
      });
    };
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    document.addEventListener("visibilitychange", load);
    return () => { active = false; window.clearInterval(timer); document.removeEventListener("visibilitychange", load); };
  }, []);
  if (count === null) return null;
  const label = count > 0 ? `${t("Notifications")}: ${count} ${t("new")}` : t("Notifications");
  return (
    <Link href="/notifications" className="fm-bell" aria-label={label}>
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0" />
      </svg>
      <span className="fm-bell-text">{t("Notifications")}</span>
      {count > 0 && <span className="fm-bell-count" aria-hidden="true">{count > 9 ? "9+" : count}</span>}
    </Link>
  );
}

function when(item: Notification, feed: NotificationFeed, language: string): string {
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: feed.timezone, year: "numeric", month: "2-digit", day: "2-digit" });
  const time = eventTime(item.at, feed.timezone, language);
  return day.format(new Date(item.at)) === day.format(new Date(feed.server_time))
    ? time
    : `${eventDate(day.format(new Date(item.at)), language)} · ${time}`;
}

// The list itself (/notifications): newest first; the ones that arrived since the
// last visit are marked, and opening the list marks them seen on this device.
export function NotificationList() {
  const { t, language } = useLanguage();
  const [state, setState] = useState<State>({ kind: "loading" });
  useEffect(() => {
    let active = true;
    void fetchNotifications().then((r) => {
      if (!active) return;
      if (!r.ok) {
        setState(r.error.code === "UNAUTHENTICATED" || r.error.code === "REGISTRATION_NOT_VERIFIED"
          ? { kind: "signed-out" }
          : { kind: "error", text: messageFor(r.error.code) });
        return;
      }
      setState({ kind: "ok", feed: r.data, seen: lastSeen() });
      markSeen(Date.parse(r.data.server_time));
    });
    return () => { active = false; };
  }, []);

  if (state.kind === "loading") return <p role="status" className="fm-note">{t("Loading…")}</p>;
  if (state.kind === "signed-out") {
    return <p className="fm-alert fm-alert-info">{t("Sign in to see your notifications.")} <Link href="/login">{t("Sign in")}</Link></p>;
  }
  if (state.kind === "error") return <p role="alert" className="fm-alert fm-alert-warn">{t(state.text)}</p>;
  const { feed, seen } = state;
  return (
    <section className="fm-stack" aria-label={t("Notifications")}>
      {feed.items.length === 0 ? (
        <p className="fm-note">{t("Nothing yet. Reminders, requests and points will appear here.")}</p>
      ) : (
        <ul className="fm-notes">
          {feed.items.map((item) => {
            const text = describe(item, t, feed.timezone, language);
            const fresh = Date.parse(item.at) > seen;
            return (
              <li key={item.id}>
                <Link href={item.href} className={`fm-notice${fresh ? " fm-notice-new" : ""}`}>
                  <span className="fm-notice-title">{text.title}</span>
                  {text.detail && <span className="fm-notice-detail">{text.detail}</span>}
                  <span className="fm-notice-time">
                    {fresh && <span className="fm-notice-dot">{t("New")}</span>}
                    {when(item, feed, language)}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      <AttendeeTabBar />
    </section>
  );
}
