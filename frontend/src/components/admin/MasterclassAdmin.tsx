"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useLanguage } from "@/components/ui/LanguageProvider";
import { apiGet, apiPost, apiRequest, type ApiResult } from "@/lib/api";
import { programLabel } from "@/lib/masterclasses";
import { messageFor } from "@/lib/errors";

type Row = Record<string, unknown>;
type ProgramRows = { events: Row[]; blocks: Row[]; rooms: Row[]; masterclasses: Row[] };
type Person = { reservation_id: number; name: string; email: string; position?: number };
type Reservations = { masterclass: Row; confirmed_count: number; waitlist_count: number; confirmed: Person[]; waitlist: Person[] };

// Why an edit was refused (`details.reason` of MASTERCLASS_ADMIN_CONFLICT).
const REASONS: Record<string, string> = {
  capacity_below_confirmed: "The capacity cannot be below the seats already confirmed.",
  waitlist_below_waiting: "The waitlist cannot be shorter than the people already waiting.",
  has_reservations: "This masterclass has reservations, so it cannot be moved or deleted.",
  active_reservations: "People hold places in this masterclass, so it cannot be hidden.",
  block_overlap: "This block would overlap another block of the event.",
  room_taken: "That room is already used in this block.",
  wrong_event: "That block or room belongs to another event.",
  duplicate: "That code or name is already in use.",
};

function failure(result: Extract<ApiResult<unknown>, { ok: false }>): string {
  const reason = result.error.details.reason;
  if (result.error.code === "MASTERCLASS_ADMIN_CONFLICT" && typeof reason === "string" && REASONS[reason]) return REASONS[reason];
  return messageFor(result.error.code);
}

const input = "min-h-11 w-full rounded-xl border border-(--fm-line-strong) bg-white p-3 text-base text-(--fm-ink)";

function Field({ label, value, set, multiline = false }: { label: string; value: string; set: (v: string) => void; multiline?: boolean }) {
  const { t } = useLanguage();
  return (
    <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">
      {t(label)}
      {multiline
        ? <textarea className={input} rows={3} value={value} onChange={(e) => set(e.target.value)} />
        : <input className={input} value={value} onChange={(e) => set(e.target.value)} />}
    </label>
  );
}

const int = (v: string) => (v.trim() === "" ? null : Number(v.trim()));
const text = (v: string) => (v.trim() === "" ? null : v.trim());

// The masterclass section of the admin panel: the programme with confirmed /
// waiting counts, who holds a seat and who waits (in queue order), and forms for
// events, blocks, rooms and masterclasses. Every change is checked and audited by
// the server; an edit that would break reservations is refused with its reason.
export function MasterclassAdmin() {
  const { t, language } = useLanguage();
  const [rows, setRows] = useState<ProgramRows | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [lists, setLists] = useState<Reservations | null>(null);

  const [mcId, setMcId] = useState("");
  const [eventId, setEventId] = useState("");
  const [blockId, setBlockId] = useState("");
  const [roomId, setRoomId] = useState("");
  const [title, setTitle] = useState("");
  const [speaker, setSpeaker] = useState("");
  const [description, setDescription] = useState("");
  const [capacity, setCapacity] = useState("");
  const [waitlist, setWaitlist] = useState("");
  // "" = leave as it is (an edit), "true" / "false" = show / hide.
  const [published, setPublished] = useState("");

  const [blkId, setBlkId] = useState("");
  const [blkCode, setBlkCode] = useState("");
  const [blockNumber, setBlockNumber] = useState("");
  const [blockLabelMode, setBlockLabelMode] = useState("");
  const [roomNumber, setRoomNumber] = useState("");
  const [blkTitle, setBlkTitle] = useState("");
  const [starts, setStarts] = useState("");
  const [ends, setEnds] = useState("");
  const [deadline, setDeadline] = useState("");
  const [editRoomId, setEditRoomId] = useState("");
  const [roomLabelMode, setRoomLabelMode] = useState("");
  const [roomName, setRoomName] = useState("");
  const [evCode, setEvCode] = useState("");
  const [evTitle, setEvTitle] = useState("");
  const [evVenue, setEvVenue] = useState("");
  const [evZone, setEvZone] = useState("");
  const [evDate, setEvDate] = useState("");

  const apply = useCallback((result: ApiResult<ProgramRows>) => {
    if (result.ok) { setRows(result.data); setError(""); } else setError(messageFor(result.error.code));
  }, []);
  const load = useCallback(
    () => apiGet<ProgramRows>("/api/admin/masterclass-program").then(apply),
    [apply],
  );
  useEffect(() => {
    let active = true;
    void apiGet<ProgramRows>("/api/admin/masterclass-program").then((result) => {
      if (active) apply(result);
    });
    return () => { active = false; };
  }, [apply]);

  async function change(run: () => Promise<ApiResult<unknown>>, done = "Change saved and recorded in the admin audit.") {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setNotice(""); setError("");
    const result = await run();
    if (result.ok) {
      const promoted = (result.data as Row | null)?.promoted_from_waitlist;
      setNotice(typeof promoted === "number" && promoted > 0 ? t("Larger capacity: {n} people moved from the waitlist to a seat.").replace("{n}", String(promoted)) : t(done));
      await load();
    } else setError(failure(result));
    inFlight.current = false; setBusy(false);
  }

  async function show(id: number) {
    const result = await apiGet<Reservations>(`/api/admin/masterclasses/${id}/reservations`);
    if (result.ok) setLists(result.data); else setError(messageFor(result.error.code));
  }

  function saveMasterclass() {
    const id = int(mcId);
    if (id === null) {
      void change(() => apiPost("/api/admin/masterclasses", {
        event_id: int(eventId), block_id: int(blockId), room_id: int(roomId), title: title.trim(),
        speaker: text(speaker), description: text(description),
        ...(int(capacity) !== null ? { capacity: int(capacity) } : {}),
        ...(int(waitlist) !== null ? { waitlist_capacity: int(waitlist) } : {}),
        is_published: published === "true",
      }));
      return;
    }
    // An edit sends only what was filled in; the server keeps the rest.
    const body: Row = {};
    if (published !== "") body.is_published = published === "true";
    if (blockId.trim() !== "") body.block_id = int(blockId);
    if (roomId.trim() !== "") body.room_id = int(roomId);
    if (title.trim() !== "") body.title = title.trim();
    if (speaker.trim() !== "") body.speaker = speaker.trim();
    if (description.trim() !== "") body.description = description.trim();
    if (capacity.trim() !== "") body.capacity = int(capacity);
    if (waitlist.trim() !== "") body.waitlist_capacity = int(waitlist);
    void change(() => apiRequest(`/api/admin/masterclasses/${id}`, "PATCH", body));
  }

  function saveBlock() {
    const number = int(blockNumber);
    if ((blockNumber.trim() !== "" && (!Number.isInteger(number) || number === null || number < 1 || number > 10000)) || (blockLabelMode === "standard" && number === null)) { setError("Enter a standard number between 1 and 10000."); return; }
    const id = int(blkId);
    if (id === null) {
      void change(() => apiPost("/api/admin/masterclass-blocks", {
        event_id: int(eventId), code: blkCode.trim(), title: blkTitle.trim(),
        starts_at: starts.trim(), ends_at: ends.trim(), change_deadline: deadline.trim(),
        display_number: int(blockNumber),
      }));
      return;
    }
    const body: Row = {};
    if (blockLabelMode !== "") body.display_number = blockLabelMode === "custom" ? null : int(blockNumber);
    if (blkTitle.trim() !== "") body.title = blkTitle.trim();
    if (starts.trim() !== "") body.starts_at = starts.trim();
    if (ends.trim() !== "") body.ends_at = ends.trim();
    if (deadline.trim() !== "") body.change_deadline = deadline.trim();
    void change(() => apiRequest(`/api/admin/masterclass-blocks/${id}`, "PATCH", body));
  }

  function saveRoom() {
    const number = int(roomNumber);
    if ((roomNumber.trim() !== "" && (!Number.isInteger(number) || number === null || number < 1 || number > 10000)) || (roomLabelMode === "standard" && number === null)) { setError("Enter a standard number between 1 and 10000."); return; }
    const id = int(editRoomId);
    if (id === null) { void change(() => apiPost("/api/admin/masterclass-rooms", { event_id: int(eventId), name: roomName.trim(), display_number: int(roomNumber) })); return; }
    const body: Row = {};
    if (roomName.trim()) body.name = roomName.trim();
    if (roomLabelMode !== "") body.display_number = roomLabelMode === "custom" ? null : int(roomNumber);
    void change(() => apiRequest(`/api/admin/masterclass-rooms/${id}`, "PATCH", body));
  }

  const blockTitle = (id: unknown) => {
    const block = rows?.blocks.find(b => b.id === id);
    return block ? programLabel(String(block.title), typeof block.display_number === "number" ? block.display_number : null, "block", language) : t("Not scheduled");
  };
  const roomTitle = (id: unknown) => {
    const room = rows?.rooms.find(r => r.id === id);
    return room ? programLabel(String(room.name), typeof room.display_number === "number" ? room.display_number : null, "room", language) : "—";
  };

  return (
    <div className="grid min-w-0 gap-5 [overflow-wrap:anywhere]">
      {rows === null && !error && <p role="status" className="text-sm text-(--fm-ink-soft)">{t("Loading records…")}</p>}
      {rows && (
        <>
          <div className="grid gap-1 text-sm text-(--fm-ink-soft)">
            <p><span className="font-semibold text-(--fm-ink)">{t("Events")}:</span> {rows.events.map((e) => `#${String(e.id)} ${String(e.title)} (${String(e.event_date)}, ${String(e.timezone)})`).join(" · ") || "—"}</p>
            <p><span className="font-semibold text-(--fm-ink)">{t("Blocks")}:</span> {rows.blocks.map((b) => `#${String(b.id)} ${blockTitle(b.id)}`).join(" · ") || "—"}</p>
            <p><span className="font-semibold text-(--fm-ink)">{t("Rooms")}:</span> {rows.rooms.map((r) => `#${String(r.id)} ${roomTitle(r.id)}`).join(" · ") || "—"}</p>
          </div>
          <div className="min-w-0 max-w-full overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead className="text-(--fm-muted)"><tr><th className="p-2">ID</th><th className="p-2">{t("Title")}</th><th className="p-2">{t("Blocks")}</th><th className="p-2">{t("Rooms")}</th><th className="p-2">{t("Confirmed")}</th><th className="p-2">{t("Waiting")}</th><th className="p-2" /></tr></thead>
              <tbody>
                {rows.masterclasses.map((m) => (
                  <tr key={String(m.id)} className="border-t border-(--fm-line)">
                    <td className="p-2">{String(m.id)}</td>
                    <td className="p-2">{String(m.title)} <span className="text-(--fm-muted)">· {m.is_published ? t("Published") : t("Hidden")}</span></td>
                    <td className="p-2">{blockTitle(m.block_id)}</td>
                    <td className="p-2">{roomTitle(m.room_id)}</td>
                    <td className="p-2">{String(m.confirmed_count)}/{String(m.capacity)}</td>
                    <td className="p-2">{String(m.waitlist_count)}/{String(m.waitlist_capacity)}</td>
                    <td className="p-2"><button className="min-h-11 rounded-xl border border-(--fm-line-strong) bg-(--fm-tint) px-3" onClick={() => void show(Number(m.id))}>{t("Show reservations")}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {lists && (
        <section className="grid gap-2 rounded-2xl border border-(--fm-line) p-4" aria-label={t("Show reservations")}>
          <h3 className="font-semibold">{String(lists.masterclass.title)}</h3>
          <p className="text-sm text-(--fm-ink-soft)">{t("Confirmed seats")}: {lists.confirmed_count} · {t("Waitlist")}: {lists.waitlist_count}</p>
          <h4 className="text-sm font-semibold">{t("Confirmed seats")}</h4>
          {lists.confirmed.length === 0 ? <p className="text-sm text-(--fm-muted)">{t("Nobody yet.")}</p> : <ol className="list-decimal pl-6 text-sm">{lists.confirmed.map((p) => <li key={p.reservation_id}>{p.name} · {p.email}</li>)}</ol>}
          <h4 className="text-sm font-semibold">{t("Waitlist")}</h4>
          {lists.waitlist.length === 0 ? <p className="text-sm text-(--fm-muted)">{t("Nobody yet.")}</p> : <ol className="list-decimal pl-6 text-sm">{lists.waitlist.map((p) => <li key={p.reservation_id}>{p.name} · {p.email}</li>)}</ol>}
        </section>
      )}

      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 disabled:opacity-60">
        <legend className="px-1 text-lg font-medium">{t("Add or edit a masterclass")}</legend>
        <Field label="Masterclass ID (empty to add a new one)" value={mcId} set={setMcId} />
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Block ID (empty: not scheduled)" value={blockId} set={setBlockId} />
        <Field label="Room ID" value={roomId} set={setRoomId} />
        <Field label="Title" value={title} set={setTitle} />
        <Field label="Speaker" value={speaker} set={setSpeaker} />
        <Field label="Description" value={description} set={setDescription} multiline />
        <Field label="Capacity" value={capacity} set={setCapacity} />
        <Field label="Waitlist size" value={waitlist} set={setWaitlist} />
        <label className="flex flex-col gap-1 text-sm text-(--fm-ink-soft)">{t("Visible to attendees")}
          <select className={input} value={published} onChange={(e) => setPublished(e.target.value)}>
            <option value="">{t("No change (new: hidden)")}</option>
            <option value="true">{t("Published")}</option>
            <option value="false">{t("Hidden")}</option>
          </select>
        </label>
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={saveMasterclass}>{t("Save masterclass")}</button>
        <button className="rounded bg-(--fm-red-soft) p-2" onClick={() => {
          const id = int(mcId);
          if (id !== null && window.confirm(t("Confirm this change?"))) void change(() => apiRequest(`/api/admin/masterclasses/${id}`, "DELETE"));
        }}>{t("Delete masterclass")}</button>
      </fieldset>

      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 disabled:opacity-60">
        <legend className="px-1 text-lg font-medium">{t("Add a block")}</legend>
        <Field label="Block ID (empty to add a new one)" value={blkId} set={setBlkId} />
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Code" value={blkCode} set={setBlkCode} />
        <Field label="Title" value={blkTitle} set={setBlkTitle} />
        <Field label="Standard block number (optional)" value={blockNumber} set={setBlockNumber} />
        <label>{t("Block label")}<select className={input} value={blockLabelMode} onChange={e => setBlockLabelMode(e.target.value)}><option value="">{t("Keep current label")}</option><option value="custom">{t("Use custom name")}</option><option value="standard">{t("Apply standard number")}</option></select></label>
        <Field label="Starts (with offset, e.g. 2026-11-13T11:30+04:00)" value={starts} set={setStarts} />
        <Field label="Ends" value={ends} set={setEnds} />
        <Field label="Change deadline" value={deadline} set={setDeadline} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={saveBlock}>{t("Save block")}</button>
      </fieldset>

      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 disabled:opacity-60">
        <legend className="px-1 text-lg font-medium">{t("Add a room")}</legend>
        <Field label="Event ID" value={eventId} set={setEventId} />
        <Field label="Room ID (empty to add a new one)" value={editRoomId} set={setEditRoomId} />
        <Field label="Room name" value={roomName} set={setRoomName} />
        <Field label="Standard room number (optional)" value={roomNumber} set={setRoomNumber} />
        <p>{t("Leave empty to show the custom name.")}</p>
        <label>{t("Room label")}<select className={input} value={roomLabelMode} onChange={e => setRoomLabelMode(e.target.value)}><option value="">{t("Keep current label")}</option><option value="custom">{t("Use custom name")}</option><option value="standard">{t("Apply standard number")}</option></select></label>
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={saveRoom}>{t("Save room")}</button>
      </fieldset>

      <fieldset disabled={busy} className="grid max-w-lg gap-3 rounded-2xl border border-(--fm-line) bg-white p-5 disabled:opacity-60">
        <legend className="px-1 text-lg font-medium">{t("Add an event")}</legend>
        <Field label="Code" value={evCode} set={setEvCode} />
        <Field label="Title" value={evTitle} set={setEvTitle} />
        <Field label="Venue" value={evVenue} set={setEvVenue} />
        <Field label="Time zone (e.g. Asia/Baku)" value={evZone} set={setEvZone} />
        <Field label="Date (YYYY-MM-DD)" value={evDate} set={setEvDate} />
        <button className="rounded bg-(--fm-red) text-white p-2" onClick={() => void change(() => apiPost("/api/admin/masterclass-events", { code: evCode.trim(), title: evTitle.trim(), venue: text(evVenue), timezone: evZone.trim(), event_date: evDate.trim() }))}>{t("Save event")}</button>
      </fieldset>

      {notice && <p role="status" className="rounded-xl border border-(--fm-ok-line) bg-(--fm-ok-soft) p-3 text-sm text-(--fm-ok)">{t(notice)}</p>}
      {error && <p role="alert" className="text-(--fm-warn)">{t(error)}</p>}
    </div>
  );
}
