"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiPost, type ApiResult } from "@/lib/api";
import { cardTokenFrom } from "@/lib/competition";
import { messageFor } from "@/lib/errors";
import { eventTime } from "@/lib/masterclasses";

type Pending = {
  id: number;
  kind: string;
  target_id: number;
  phase: string | null;
  attendee: string;
  scanned_by: string;
  device_scanned_at: string;
  received_at: string;
  card_evidence: boolean;
  own_scan: boolean;
};
type Decision = { status: string; outcome: { reason?: string } | null };

const KIND_LABEL: Record<string, string> = {
  masterclass_door: "Masterclass door",
  playground_complete: "End of a Playground session",
  mentor_complete: "End of a mentor group",
};

// Why the server refused or did not apply a decision (competition/offline.py).
const SELF_REVIEW = "You made this scan; another Game Master must review it.";
const NEEDS_CARD = "This scan was saved before card checks existed: scan the attendee's current card to review it.";
export const REVIEW_TEXT: Record<string, string> = {
  self_review: SELF_REVIEW,
  verified_after_received: "The verified time cannot be later than when the server received the scan.",
  card_evidence_required: NEEDS_CARD,
  card_mismatch: "That card belongs to someone else.",
  card_revoked: "Not applied: the attendee's card was replaced or revoked.",
  attendee_inactive: "Not applied: the attendee's account is not active.",
  scanner_inactive: "Not applied: the account that made the scan is no longer active.",
  scanner_not_authorized: "Not applied: the account that made the scan no longer has that job.",
  after_close: "Not applied: the verified time is after the competition closed.",
  outside_window: "Not applied: the verified time is outside the scan window.",
};

// "HH:MM" on the scan's own event day, in the event's time zone, as ISO with offset.
export function verifiedIso(claimedIso: string, hhmm: string, timeZone: string): string | null {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(hhmm.trim());
  if (!match) return null;
  const claimed = new Date(claimedIso);
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", timeZoneName: "longOffset" }).formatToParts(claimed);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const offset = get("timeZoneName").replace("GMT", "") || "+00:00";
  return `${get("year")}-${get("month")}-${get("day")}T${match[1]}:${match[2]}:00${offset === "" ? "+00:00" : offset}`;
}

// The Game Master's review of scans a phone sent late. A phone's clock is not
// proof: state the time you verified (a paper list, the host's word) and how.
// The server re-checks the card, the scanner's job and every scan rule.
export function OfflineReview({ timezone }: { timezone: string }) {
  const { t, language } = useLanguage();
  const [scans, setScans] = useState<Pending[] | null>(null);
  const [reason, setReason] = useState("");
  const [times, setTimes] = useState<Record<number, string>>({});
  const [cards, setCards] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState("");
  const inFlight = useRef(false);

  const apply = useCallback((r: ApiResult<{ scans: Pending[] }>) => setScans(r.ok && Array.isArray(r.data.scans) ? r.data.scans : null), []);
  useEffect(() => {
    let active = true;
    void apiGet<{ scans: Pending[] }>("/api/staff/offline-scans").then((r) => {
      if (active) apply(r);
    });
    return () => { active = false; };
  }, [apply]);

  async function decide(s: Pending, verb: "approve" | "reject") {
    if (inFlight.current) return;
    if (reason.trim().length < 3) { setNotice("Write how you verified it (at least 3 characters)."); return; }
    const body: Record<string, unknown> = { reason: reason.trim() };
    if (verb === "approve") {
      const at = verifiedIso(s.device_scanned_at, times[s.id] ?? "", timezone);
      if (!at) { setNotice("Enter the time you verified, as HH:MM."); return; }
      body.verified_at = at;
      if (!s.card_evidence) {
        const token = cardTokenFrom(cards[s.id] ?? "");
        if (!token) { setNotice(NEEDS_CARD); return; }
        body.card_token = token;
      }
    }
    inFlight.current = true;
    try {
      const r = await apiPost<Decision>(`/api/staff/offline-scans/${s.id}/${verb}`, body);
      if (!r.ok) {
        const why = r.error.details.reason;
        setNotice(typeof why === "string" && REVIEW_TEXT[why] ? REVIEW_TEXT[why] : messageFor(r.error.code));
      } else if (r.data.status === "failed") {
        const why = r.data.outcome?.reason ?? "";
        setNotice(REVIEW_TEXT[why] ?? `${t("Not applied:")} ${why}`);
      } else setNotice(r.data.status === "applied" ? "Applied." : "Rejected.");
      apply(await apiGet<{ scans: Pending[] }>("/api/staff/offline-scans"));
    } finally {
      inFlight.current = false;
    }
  }

  if (scans === null) return null;
  return (
    <section className="fm-section" aria-labelledby="offline-review">
      <h2 id="offline-review" className="fm-eyebrow">{t("Late scans to review")}</h2>
      {scans.length === 0 ? <p className="fm-empty">{t("Nothing waiting.")}</p> : (
        <>
          <label className="fm-field">
            <span className="fm-label">{t("How you verified it")}</span>
            <input className="fm-input" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} />
          </label>
          <ul className="fm-list">
            {scans.map((s) => (
              <li key={s.id} className="fm-card fm-section" data-testid={`offline-${s.id}`}>
                <p className="fm-strong">{s.attendee} · {t(KIND_LABEL[s.kind] ?? s.kind)}{s.phase ? ` (${s.phase})` : ""}</p>
                <p className="fm-small">{t("Phone time {a}, received {b}, by {who}").replace("{a}", eventTime(s.device_scanned_at, timezone, language)).replace("{b}", eventTime(s.received_at, timezone, language)).replace("{who}", s.scanned_by)}</p>
                {s.own_scan ? (
                  <p className="fm-micro">{t(SELF_REVIEW)}</p>
                ) : (
                  <>
                    <label className="fm-field">
                      <span className="fm-label">{t("Verified time (HH:MM)")}</span>
                      <input className="fm-input" inputMode="numeric" placeholder="15:52" value={times[s.id] ?? ""} onChange={(e) => setTimes({ ...times, [s.id]: e.target.value })} />
                    </label>
                    {!s.card_evidence && (
                      <label className="fm-field">
                        <span className="fm-label">{t("Attendee's current card code (re-scan)")}</span>
                        <input className="fm-input" value={cards[s.id] ?? ""} onChange={(e) => setCards({ ...cards, [s.id]: e.target.value })} />
                      </label>
                    )}
                    <button type="button" className="fm-btn fm-btn-primary fm-btn-block" onClick={() => void decide(s, "approve")}>{t("Approve")}</button>
                    <button type="button" className="fm-btn fm-btn-secondary fm-btn-block" onClick={() => void decide(s, "reject")}>{t("Reject")}</button>
                  </>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {notice && <p role="status" className="fm-alert fm-alert-info">{t(notice)}</p>}
    </section>
  );
}
