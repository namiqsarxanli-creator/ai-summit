"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttendeeTabBar } from "@/components/nav/AttendeeTabBar";
import { useLanguage } from "@/components/ui/LanguageProvider";
import { fetchBooths, type Booth } from "@/lib/matching";

// The rules of the day (FACEMARK's participant guide), told the way the app really
// works: the attendee scans the Main Stage screen; a volunteer scans the attendee's
// QR card at a masterclass door and a booth. Readable without signing in. The booth
// games come from the server, so a new booth appears here by itself.

const BEFORE = [
  { title: "Sign up with your link", detail: "Open the app with the link Facemark sends you." },
  { title: "Fill in your interest card", detail: "Your field, your interests and who you want to meet. +5 points." },
  { title: "Choose your sessions", detail: "Pick your masterclasses and AI Playground sessions in advance. Rooms hold 40 people." },
] as const;

const ROUTE = [
  { title: "Panel 1 · The future of AI", time: "Shusha Hall · 10:20–11:30", tag: "8 points", detail: "Scan the entry code on the big screen in the first 15 minutes and the exit code in the last 10 minutes. Both are needed." },
  { title: "Qmeter questions", time: "During the panels", tag: "Up to 4 points", detail: "Ask your question in Qmeter. Points are recorded by name and phone number, so use the details you have in the app." },
  { title: "Masterclass 1", time: "11:30–13:00", tag: "12 points", detail: "Go to the masterclass you chose. Show your QR card to the volunteer at the door when you enter and when you leave." },
  { title: "AI Playground", time: "09:00–18:00", tag: "Up to 24 points", detail: "Come to the booth at your 15-minute session, alone or in a team of 2–3, and play. At the end, show your QR card to be scanned." },
  { title: "Connection Hub", time: "All day", tag: "Up to 12 points", detail: "Meet new people and scan each other's QR in People. The same person counts only once." },
  { title: "Panel 2 · Humans and AI", time: "Shusha Hall · 14:00–15:30", tag: "8 points", detail: "Scan the entry and exit codes the same way as in Panel 1." },
  { title: "Masterclass 2", time: "16:00–17:30", tag: "12 points", detail: "Show your QR card at the door when you enter and when you leave." },
  { title: "Grand Final", time: "Shusha Hall · code open 17:25–17:45", tag: "Founder", detail: "Scan the code on the screen as you enter the hall: it confirms you stayed to the end. Then the final game and the Lucky Draw." },
] as const;

const POINTS = [
  { title: "Profile and interest card", detail: "Before the event", points: 5 },
  { title: "Main Stage panels", detail: "2 panels · entry + exit", points: 16 },
  { title: "Questions in Qmeter", detail: "2 points per panel", points: 4 },
  { title: "Masterclasses", detail: "2 masterclasses · entry + exit", points: 24 },
  { title: "AI Playground", detail: "8 different booths · 3 points each", points: 24 },
  { title: "Connections", detail: "Your best 4: a recommended person 3, anyone else 1, a colleague 0", points: 12 },
  { title: "Beyond Now Founder", detail: "4 zones + Grand Final", points: 15 },
] as const;

const RULES = [
  { title: "Entry and exit count together", detail: "If only one is scanned, that panel or masterclass earns no points." },
  { title: "Your code is yours", detail: "Scan only for yourself. Points written in someone else's name are cancelled." },
  { title: "Watch the time windows", detail: "Panels: the entry code works in the first 15 minutes, the exit code in the last 10. Masterclasses: entry in the first 10 minutes, exit in the last 10." },
  { title: "Do not be late for a booth", detail: "More than 3 minutes late, your place goes to the queue." },
  { title: "Plan your booths", detail: "The free times are 09:00–10:15, 13:00–14:00 and 15:30–16:00. A session during a panel costs you that panel's points." },
  { title: "Keep your phone charged", detail: "Points can be earned until 17:30; the Grand Final code comes after that." },
] as const;

const QUESTIONS = [
  { title: "The code on the screen does not scan.", detail: "Type the six digits under the QR into “Code from the screen” in My points and press Send, or ask a volunteer." },
  { title: "My QR card does not scan.", detail: "Tell the volunteer the six characters under your QR code." },
  { title: "My points look wrong.", detail: "Go to the Facemark team at the registration desk." },
] as const;

// What a booth's visitor does, where the booth wrote more than its one-line card.
const BOOTH_TASK: Record<string, string> = {
  "global-academy-prompt-master": "Using AI, complete a three-stage challenge in 10 minutes: write a professional email, solve a logic problem and create a creative concept for the school of the future. Each stage is scored against set criteria, and you finish with an internal result out of 100.",
};

const companyAndGame = (name: string): [string, string | null] => {
  const at = name.indexOf(" — ");
  return at > 0 ? [name.slice(0, at), name.slice(at + 3)] : [name, null];
};

export function GameGuide() {
  const { t } = useLanguage();
  const [booths, setBooths] = useState<Booth[] | null>(null);
  useEffect(() => {
    let active = true;
    void fetchBooths().then((r) => { if (active) setBooths(r.ok ? r.data : []); });
    return () => { active = false; };
  }, []);

  return (
    <div className="fm-stack-lg">
      <section className="fm-card fm-section fm-guide-stats" aria-label={t("The game in numbers")}>
        <p><strong>100</strong><span>{t("points at most")}</span></p>
        <p><strong>4</strong><span>{t("zones for Founder")}</span></p>
        <p><strong>1</strong><span>{t("Lucky Draw")}</span></p>
      </section>

      <section className="fm-section" aria-labelledby="guide-before">
        <h2 id="guide-before" className="fm-eyebrow">{t("Before the event")}</h2>
        <ol className="fm-list">
          {BEFORE.map((s) => (
            <li key={s.title} className="fm-card fm-section">
              <h3 className="fm-h3">{t(s.title)}</h3>
              <p className="fm-small">{t(s.detail)}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="fm-section" aria-labelledby="guide-route">
        <h2 id="guide-route" className="fm-eyebrow">{t("Your route on the day")}</h2>
        <p className="fm-small">{t("Keep the app open on your phone. To scan the big screen, open My points and press Scan with the camera.")}</p>
        <ol className="fm-list">
          {ROUTE.map((s) => (
            <li key={s.title} className="fm-card fm-section">
              <div className="fm-row-between">
                <h3 className="fm-h3">{t(s.title)}</h3>
                <span className="fm-badge">{t(s.tag)}</span>
              </div>
              <p className="fm-micro">{t(s.time)}</p>
              <p className="fm-small">{t(s.detail)}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="fm-section" aria-labelledby="guide-points">
        <h2 id="guide-points" className="fm-eyebrow">{t("How 100 points add up")}</h2>
        <ul className="fm-list fm-card fm-section">
          {POINTS.map((p) => (
            <li key={p.title} className="fm-row-between fm-guide-row">
              <span>
                <span className="fm-strong">{t(p.title)}</span>
                <br />
                <span className="fm-micro">{t(p.detail)}</span>
              </span>
              <span className="fm-badge">{p.points}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="fm-card fm-section" aria-labelledby="guide-founder">
        <h2 id="guide-founder" className="fm-eyebrow">{t("Beyond Now Founder")}</h2>
        <p className="fm-h3">{t("+15 points and a place in the Lucky Draw")}</p>
        <p className="fm-small">{t("Earn points in each of the four zones (Main Stage, Masterclass, AI Playground, Connection Hub) and scan the code as you enter the Grand Final. To win a prize you must be a Founder and be in the hall.")}</p>
      </section>

      <section className="fm-section" aria-labelledby="guide-games">
        <h2 id="guide-games" className="fm-eyebrow">{t("AI Playground games")}</h2>
        {booths === null && <p role="status" className="fm-note">{t("Loading…")}</p>}
        <ul className="fm-list">
          {(booths ?? []).map((b) => {
            const [company, game] = companyAndGame(t(b.name));
            const task = BOOTH_TASK[b.slug];
            return (
              <li key={b.slug} className="fm-card fm-section">
                <h3 className="fm-h3">{company}</h3>
                {game && <p className="fm-strong">{game}</p>}
                {b.description && <p className="fm-small">{t(b.description)}</p>}
                {task && <p className="fm-small">{t(task)}</p>}
                <Link className="fm-link fm-small" href={`/playground?booth=${encodeURIComponent(b.slug)}`}>{t("Book a 15-minute session")}</Link>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="fm-section" aria-labelledby="guide-rules">
        <h2 id="guide-rules" className="fm-eyebrow">{t("Important rules")}</h2>
        <ul className="fm-list">
          {RULES.map((r) => (
            <li key={r.title} className="fm-card fm-section">
              <h3 className="fm-h3">{t(r.title)}</h3>
              <p className="fm-small">{t(r.detail)}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="fm-section" aria-labelledby="guide-faq">
        <h2 id="guide-faq" className="fm-eyebrow">{t("Frequently asked questions")}</h2>
        <ul className="fm-list">
          {QUESTIONS.map((q) => (
            <li key={q.title} className="fm-card fm-section">
              <h3 className="fm-h3">{t(q.title)}</h3>
              <p className="fm-small">{t(q.detail)}</p>
            </li>
          ))}
        </ul>
      </section>
      <AttendeeTabBar />
    </div>
  );
}

/** Every text of the page, for the test that guards its translations. */
export const GUIDE_TEXTS: string[] = [
  ...[...BEFORE, ...RULES, ...QUESTIONS].flatMap((x) => [x.title, x.detail]),
  ...ROUTE.flatMap((x) => [x.title, x.time, x.tag, x.detail]),
  ...POINTS.flatMap((x) => [x.title, x.detail]),
  ...Object.values(BOOTH_TASK),
];
