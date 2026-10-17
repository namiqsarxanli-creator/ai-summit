import { afterEach, describe, expect, it, vi } from "vitest";

import { hasTranslation } from "./i18n";
import { masterclassTranslations } from "./i18n-masterclass";
import {
  actionFor,
  cancelChoice,
  chooseMasterclass,
  eventDate,
  programLabel,
  eventTime,
  type Block,
  type Choice,
  type MasterclassItem,
} from "./masterclasses";

const item = (id: number, availability: MasterclassItem["availability"]): MasterclassItem => ({
  id,
  title: `M${id}`,
  description: null,
  speaker: null,
  room: null,
  capacity: 40,
  waitlist_capacity: 10,
  confirmed_count: 0,
  waitlist_count: 0,
  seats_left: 40,
  availability,
});
const block = (open = true): Block => ({
  id: 1,
  code: "b1",
  title: "Blok 1",
  starts_at: "2026-11-13T07:30:00Z",
  ends_at: "2026-11-13T09:00:00Z",
  change_deadline: "2026-11-13T07:00:00Z",
  changes_open: open,
  masterclasses: [],
});
const mine = (masterclass_id: number, status: Choice["status"]): Choice => ({
  block_id: 1,
  masterclass_id,
  status,
  waitlist_position: status === "waitlisted" ? 2 : null,
  change_deadline: "2026-11-13T07:00:00Z",
  changes_open: true,
});

describe("actionFor", () => {
  it("offers what the server would allow, never more", () => {
    expect(actionFor(block(), item(1, "available"), undefined)).toEqual({ kind: "choose" });
    expect(actionFor(block(), item(1, "waitlist"), undefined)).toEqual({ kind: "waitlist" });
    expect(actionFor(block(), item(1, "full"), undefined)).toEqual({ kind: "none", reason: "full" });
    expect(actionFor(block(), item(1, "available"), mine(1, "confirmed"))).toEqual({ kind: "cancel" });
    expect(actionFor(block(), item(2, "available"), mine(1, "confirmed"))).toEqual({ kind: "change" });
    expect(actionFor(block(), item(2, "waitlist"), mine(1, "confirmed"))).toEqual({
      kind: "change-to-waitlist",
      givesUpSeat: true,
    });
    expect(actionFor(block(), item(2, "waitlist"), mine(1, "waitlisted"))).toEqual({
      kind: "change-to-waitlist",
      givesUpSeat: false,
    });
  });

  it("offers nothing once the server says the block is closed", () => {
    expect(actionFor(block(false), item(1, "available"), undefined)).toEqual({ kind: "none", reason: "closed" });
    expect(actionFor(block(false), item(1, "available"), mine(1, "confirmed"))).toEqual({ kind: "none", reason: "closed" });
  });
});

describe("event times", () => {
  it("are shown in the event's time zone, whatever the phone's zone is", () => {
    expect(eventTime("2026-11-13T07:30:00Z", "Asia/Baku", "az")).toBe("11:30");
    expect(eventTime("2026-11-13T12:00:00Z", "Asia/Baku", "en")).toBe("16:00");
    expect(eventTime("2026-11-13T11:30:00Z", "Asia/Baku", "ru")).toBe("15:30");
    expect(eventDate("2026-11-13", "en")).toBe("13 November 2026");
    expect(eventDate("2026-11-13", "az")).toBe("13 noyabr 2026");
    expect(eventDate("2026-11-13", "ru")).toMatch(/^13 ноября 2026/);
  });
});

describe("requests", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("put the choice for the block and cancel it, naming no person", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await chooseMasterclass(3, 7, { joinWaitlist: true });
    await cancelChoice(3);
    const [put, del] = fetchMock.mock.calls as unknown as [[string, RequestInit], [string, RequestInit]];
    expect(put[0]).toBe("/api/attendees/me/masterclasses/3");
    expect(put[1].method).toBe("PUT");
    expect(JSON.parse(put[1].body as string)).toEqual({
      masterclass_id: 7, expected_revision: "0",
      join_waitlist: true,
      give_up_confirmed_seat: false,
    });
    expect(del[0]).toBe("/api/attendees/me/masterclasses/3");
    expect(del[1].method).toBe("DELETE");
  });
});

describe("texts", () => {
  it("every masterclass text has Azerbaijani and Russian", () => {
    for (const [key, value] of Object.entries(masterclassTranslations)) {
      expect(hasTranslation(key)).toBe(true);
      expect(value.az.trim()).not.toBe("");
      expect(value.ru.trim()).not.toBe("");
      // placeholders survive translation
      for (const token of key.match(/\{[a-z]+\}/g) ?? []) {
        expect(value.az).toContain(token);
        expect(value.ru).toContain(token);
      }
    }
  });
});


describe("explicit standard programme labels",()=>{
  it("localises numbered blocks and rooms without translating custom names",()=>{
    expect(programLabel("Blok 1",1,"block","en")).toBe("Block 1");
    expect(programLabel("Zal 2",2,"room","ru")).toBe("Зал 2");
    expect(programLabel("Blok 1",null,"block","en")).toBe("Blok 1");
    expect(programLabel("Board room",null,"room","az")).toBe("Board room");
  });
});
