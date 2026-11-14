"use client";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { eventTime } from "@/lib/masterclasses";
import { slotLabel, type PlaygroundProgram, type PlaygroundSlot } from "@/lib/playground";

const SCAN_AFTER_END_MS = 5 * 60 * 1000;

/** The session to start on: the first still open for its end scan, by the server's clock. */
export function currentSlot(slots: PlaygroundSlot[], serverTime: string): PlaygroundSlot | undefined {
  const now = new Date(serverTime).getTime();
  return slots.find((s) => new Date(s.ends_at).getTime() + SCAN_AFTER_END_MS > now) ?? slots[0];
}

// A session of one booth. FACEMARK's scanners work every booth, so they choose the
// booth first (36 sessions each); a booth's own staff see only their sessions.
export function SlotPicker({ program, value, onChange, disabled }: {
  program: PlaygroundProgram;
  value: string;
  onChange: (slotId: string) => void;
  disabled?: boolean;
}) {
  const { t, language } = useLanguage();
  const booths = [...new Map(program.slots.map((s) => [s.booth_id, s.booth])).entries()];
  const chosen = program.slots.find((s) => String(s.id) === value);
  const boothId = chosen?.booth_id ?? booths[0]?.[0];
  const ofBooth = program.slots.filter((s) => s.booth_id === boothId);
  return (
    <>
      {booths.length > 1 && (
        <label className="fm-field">
          <span className="fm-label">{t("Booth")}</span>
          <select
            className="fm-input"
            value={boothId}
            disabled={disabled}
            onChange={(e) => {
              const next = currentSlot(program.slots.filter((s) => s.booth_id === Number(e.target.value)), program.server_time);
              if (next) onChange(String(next.id));
            }}
          >
            {booths.map(([id, name]) => <option key={id} value={id}>{t(name)}</option>)}
          </select>
        </label>
      )}
      <label className="fm-field">
        <span className="fm-label">{t("Playground slots")}</span>
        <select className="fm-input" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
          {ofBooth.map((s) => (
            <option key={s.id} value={s.id}>
              {t(s.booth)} · {slotLabel(s)} · {eventTime(s.starts_at, s.timezone, language)}
            </option>
          ))}
        </select>
      </label>
    </>
  );
}
