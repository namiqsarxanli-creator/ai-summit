import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiResult } from "@/lib/api";

import {
  claim,
  discard,
  durableQueueSupported,
  enqueue,
  flush,
  loadQueue,
  queueState,
  setQueueBackendForTests,
  type Owner,
  type QueueBackend,
  type QueuedScan,
  type Submitted,
} from "./offlineQueue";

const SCAN = { kind: "MASTERCLASS_DOOR" as const, target_id: 3, phase: "EXIT" as const, card_token: "T".repeat(43) };
const ME: Owner = { queue_owner: "a".repeat(64), dataset_generation: 4, name: "Door Volunteer" };
const OTHER: Owner = { queue_owner: "b".repeat(64), dataset_generation: 4, name: "Another Volunteer" };
const AT = new Date("2026-11-13T13:25:00Z");
const NOW = AT.getTime();
const LEGACY_KEY = "facemark.offline-scans.v1";

const ok = (status: Submitted["status"]): ApiResult<Submitted> => ({ ok: true, data: { id: 1, client_scan_id: "x", status, outcome: null } });
const fail = (status: number, code: string, retryAfterSeconds?: number): ApiResult<Submitted> => ({
  ok: false, status, error: { code, message: "", details: {} }, ...(retryAfterSeconds === undefined ? {} : { retryAfterSeconds }),
});

// The storage seam, as IndexedDB behaves: transactions run one after another
// (never interleaved), a transaction either commits or leaves nothing, and a
// commit can be made to fail on purpose.
class MemoryStore implements QueueBackend {
  value: unknown = undefined;
  failCommits = 0; // how many of the next commits fail
  failAfter = Infinity; // commits that succeed before every later one fails
  private tail: Promise<unknown> = Promise.resolve();
  transact<T>(change: (raw: unknown) => { next: unknown; result: T }): Promise<{ done: true; result: T } | { done: false }> {
    const run = this.tail.then(() => {
      const { next, result } = change(structuredClone(this.value));
      if (this.failCommits > 0 || this.failAfter <= 0) {
        this.failCommits = Math.max(0, this.failCommits - 1);
        return { done: false as const };
      }
      this.failAfter -= 1;
      this.value = structuredClone(next);
      return { done: true as const, result };
    });
    this.tail = run.catch(() => undefined);
    return run;
  }
  async snapshot(): Promise<unknown> {
    return structuredClone(this.value);
  }
}
let store: MemoryStore;

beforeEach(() => {
  window.localStorage.clear();
  store = new MemoryStore();
  setQueueBackendForTests(store);
});
afterEach(() => {
  vi.restoreAllMocks();
  setQueueBackendForTests(null);
});

describe("offline scan queue - no lost writes", () => {
  it("keeps a scan queued while a flush is waiting for the server (audit repro)", async () => {
    await enqueue(SCAN, ME, AT);
    let answer: (r: ApiResult<Submitted>) => void = () => {};
    let calls = 0;
    // The first send waits for the server; any later one finds the network gone.
    const send = () => (++calls === 1 ? new Promise<ApiResult<Submitted>>((resolve) => { answer = resolve; }) : Promise.resolve(fail(0, "NETWORK_ERROR")));
    const sending = flush(ME, NOW, send);
    await vi.waitFor(() => expect(calls).toBe(1));
    const second = await enqueue({ ...SCAN, target_id: 4 }, ME, AT); // while A is in flight
    answer(ok("pending_review"));
    await sending;
    expect((await loadQueue(NOW)).map((s) => s.client_scan_id)).toEqual([second?.client_scan_id]);
  });

  it("sends each scan once when two flushes (two tabs) run at the same time", async () => {
    await enqueue(SCAN, ME, AT);
    await enqueue({ ...SCAN, target_id: 4 }, ME, AT);
    const sent: string[] = [];
    const send = async (s: QueuedScan) => { sent.push(s.client_scan_id); await Promise.resolve(); return ok("applied"); };
    await Promise.all([flush(ME, NOW, send), flush(ME, NOW, send)]);
    expect(new Set(sent).size).toBe(sent.length); // the lease kept the other flush off it
    expect(sent).toHaveLength(2);
    expect(await loadQueue(NOW)).toEqual([]);
  });

  it("keeps every one of many concurrent enqueues", async () => {
    const results = await Promise.all(Array.from({ length: 40 }, (_, i) => enqueue({ ...SCAN, target_id: 100 + i }, ME, AT)));
    expect(results.filter(Boolean)).toHaveLength(40);
    expect(await loadQueue(NOW)).toHaveLength(40);
  });

  it("survives a reload: the queue is in storage, not in the page", async () => {
    const entry = await enqueue(SCAN, ME, AT);
    const reloaded = (await queueState(ME, NOW)).mine; // a fresh read, as after a reload
    expect(reloaded.map((s) => s.client_scan_id)).toEqual([entry?.client_scan_id]);
  });
});

describe("offline scan queue - transient failures keep the scan", () => {
  it.each([
    [0, "NETWORK_ERROR"],
    [408, "TIMEOUT"],
    [429, "RATE_LIMITED"],
    [500, "INTERNAL_ERROR"],
    [502, "BAD_GATEWAY"],
    [503, "MAINTENANCE"],
  ])("retries after HTTP %i with backoff and the same client id", async (status, code) => {
    const entry = await enqueue(SCAN, ME, AT);
    const seen: QueuedScan[] = [];
    const send = vi.fn(async (s: QueuedScan) => { seen.push(s); return seen.length === 1 ? fail(status, code) : ok("pending_review"); });
    expect((await flush(ME, NOW, send)).offline).toBe(true);
    expect(await loadQueue(NOW)).toHaveLength(1);
    await flush(ME, NOW + 4_000, send); // still backing off
    expect(send).toHaveBeenCalledTimes(1);
    expect((await flush(ME, NOW + 5_000, send)).answered).toHaveLength(1);
    expect(seen.map((s) => s.client_scan_id)).toEqual([entry?.client_scan_id, entry?.client_scan_id]);
    expect(seen[1]?.scanned_at).toBe("2026-11-13T13:25:00.000Z");
    expect(await loadQueue(NOW + 5_000)).toEqual([]);
  });

  it("waits as long as the server's Retry-After asks", async () => {
    await enqueue(SCAN, ME, AT);
    const send = vi.fn(async () => fail(429, "RATE_LIMITED", 120));
    await flush(ME, NOW, send);
    await flush(ME, NOW + 60_000, send);
    expect(send).toHaveBeenCalledTimes(1);
    await flush(ME, NOW + 120_000, send);
    expect(send).toHaveBeenCalledTimes(2);
  });

  it("stops on an ended session and keeps every scan for after the sign-in", async () => {
    await enqueue(SCAN, ME, AT);
    await enqueue({ ...SCAN, target_id: 4 }, ME, AT);
    const send = vi.fn(async () => fail(401, "UNAUTHENTICATED"));
    const report = await flush(ME, NOW, send);
    expect(report.authNeeded).toBe(true);
    expect(send).toHaveBeenCalledTimes(1);
    expect((await loadQueue(NOW)).map((s) => s.target_id)).toEqual([3, 4]);
    const after = vi.fn(async () => ok("applied"));
    await flush(ME, NOW, after); // signed in again: the lease was released
    expect(after).toHaveBeenCalledTimes(2);
  });

  it("drops an entry the server refused for good and reports it", async () => {
    await enqueue(SCAN, ME, AT);
    const report = await flush(ME, NOW, vi.fn(async () => fail(403, "STAFF_NOT_AUTHORIZED")));
    expect(report.answered[0]?.result.ok).toBe(false);
    expect(await loadQueue(NOW)).toEqual([]);
  });
});

describe("offline scan queue - identity", () => {
  it("never replays another account's scans, and keeps them for that account", async () => {
    await enqueue(SCAN, OTHER, AT);
    const send = vi.fn(async () => ok("applied"));
    await flush(ME, NOW, send);
    expect(send).not.toHaveBeenCalled();
    const state = await queueState(ME, NOW);
    expect(state.mine).toEqual([]);
    expect(state.quarantined.map((s) => s.owner?.name)).toEqual(["Another Volunteer"]);
    await flush(OTHER, NOW, send); // that account signs in again on this phone
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("quarantines scans of an earlier dataset (after a reset)", async () => {
    await enqueue(SCAN, { ...ME, dataset_generation: 3 }, AT);
    const send = vi.fn(async () => ok("applied"));
    await flush(ME, NOW, send);
    expect(send).not.toHaveBeenCalled();
    expect((await queueState(ME, NOW)).quarantined).toHaveLength(1);
  });

  it("moves first-version entries into the store, quarantined until a person decides", async () => {
    const legacy = { client_scan_id: "11111111-1111-4111-8111-111111111111", ...SCAN, scanned_at: AT.toISOString(), attempts: 0, next_try_at: 0 };
    const other = { ...legacy, client_scan_id: "22222222-2222-4222-8222-222222222222" };
    window.localStorage.setItem(LEGACY_KEY, JSON.stringify([legacy, other]));
    const send = vi.fn(async (s: QueuedScan) => { expect(s.owner).toEqual(ME); return ok("pending_review"); });
    await flush(ME, NOW, send);
    expect(send).not.toHaveBeenCalled(); // never silently as the current account
    expect((await queueState(ME, NOW)).quarantined).toHaveLength(2);
    expect(await claim([legacy.client_scan_id], ME)).toEqual({ changed: 1, saved: true });
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull(); // migrated, once
    await flush(ME, NOW, send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(await discard([other.client_scan_id])).toEqual({ changed: 1, saved: true });
    expect(await loadQueue(NOW)).toEqual([]);
  });

  it("does not drop first-version entries written while they were being moved", async () => {
    const legacy = { client_scan_id: "44444444-4444-4444-8444-444444444444", ...SCAN, scanned_at: AT.toISOString(), attempts: 0, next_try_at: 0 };
    window.localStorage.setItem(LEGACY_KEY, JSON.stringify([legacy]));
    const late = { ...legacy, client_scan_id: "55555555-5555-4555-8555-555555555555" };
    const original = store.transact.bind(store);
    store.transact = (change) => {
      // A page of the first version writes another entry while this change commits.
      window.localStorage.setItem(LEGACY_KEY, JSON.stringify([legacy, late]));
      return original(change);
    };
    await enqueue(SCAN, ME, AT);
    store.transact = original;
    expect(window.localStorage.getItem(LEGACY_KEY)).not.toBeNull(); // not removed
    const ids = (await loadQueue(NOW)).map((s) => s.client_scan_id);
    expect(ids).toEqual(expect.arrayContaining([legacy.client_scan_id, late.client_scan_id]));
  });

  it("does not re-file another account's scans as mine", async () => {
    const theirs = await enqueue(SCAN, OTHER, AT);
    expect(await claim([theirs?.client_scan_id ?? ""], ME)).toEqual({ changed: 0, saved: true });
    expect((await queueState(OTHER, NOW)).mine).toHaveLength(1);
  });

  it("sends the owner label and dataset with each scan", async () => {
    await enqueue(SCAN, ME, AT);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ id: 1, client_scan_id: "x", status: "applied", outcome: null }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await flush(ME, NOW);
    const body = JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(body.queue_owner).toBe(ME.queue_owner);
    expect(body.dataset_generation).toBe(4);
    vi.unstubAllGlobals();
  });
});

describe("offline scan queue - no safe storage, no false promises", () => {
  it("queues nothing and sends nothing without IndexedDB", async () => {
    setQueueBackendForTests(null); // jsdom has no indexedDB: the real default here
    expect(durableQueueSupported()).toBe(false);
    expect(await enqueue(SCAN, ME, AT)).toBeNull(); // never reported as saved
    const send = vi.fn(async () => ok("applied"));
    const report = await flush(ME, NOW, send);
    expect(report.unsupported).toBe(true);
    expect(send).not.toHaveBeenCalled();
  });

  it("reports a store that throws as not done", async () => {
    setQueueBackendForTests({ transact: () => Promise.reject(new Error("QuotaExceededError")), snapshot: async () => undefined });
    expect(await enqueue(SCAN, ME, AT)).toBeNull();
  });

  it("stops sending a scan older than a day but never drops it silently", async () => {
    const old = new Date("2026-11-12T08:00:00Z");
    const entry = await enqueue(SCAN, ME, old);
    const later = old.getTime() + 25 * 60 * 60 * 1000;
    const send = vi.fn(async () => ok("applied"));
    await flush(ME, later, send);
    expect(send).not.toHaveBeenCalled();
    const state = await queueState(ME, later);
    expect(state.mine).toEqual([]);
    expect(state.expired.map((s) => s.client_scan_id)).toEqual([entry?.client_scan_id]);
    expect(await discard([entry?.client_scan_id ?? ""])).toEqual({ changed: 1, saved: true }); // a person decides
  });
});

describe("offline scan queue - failed writes are never reported as done", () => {
  it("does not report a scan as queued when the commit fails", async () => {
    store.failCommits = 1;
    expect(await enqueue(SCAN, ME, AT)).toBeNull();
    expect(await loadQueue(NOW)).toEqual([]);
  });

  it("sends nothing when the lease cannot be written", async () => {
    await enqueue(SCAN, ME, AT);
    store.failCommits = 1;
    const send = vi.fn(async () => ok("applied"));
    const report = await flush(ME, NOW, send);
    expect(report.storageFailed).toBe(true);
    expect(send).not.toHaveBeenCalled();
    expect(await loadQueue(NOW)).toHaveLength(1);
  });

  it("keeps a scan whose retry state cannot be written, and tries it again later", async () => {
    const entry = await enqueue(SCAN, ME, AT);
    store.failAfter = 1; // the lease commits, the backoff does not
    const report = await flush(ME, NOW, vi.fn(async () => fail(500, "INTERNAL_ERROR")));
    expect(report.storageFailed).toBe(true);
    store.failAfter = Infinity;
    expect((await loadQueue(NOW)).map((s) => s.client_scan_id)).toEqual([entry?.client_scan_id]);
    const later = vi.fn(async () => ok("applied"));
    await flush(ME, NOW + 31_000, later); // after the lease expired
    expect(later).toHaveBeenCalledTimes(1);
  });

  it("an answered scan that cannot be removed is replayed with the same id, never lost", async () => {
    const entry = await enqueue(SCAN, ME, AT);
    store.failAfter = 1; // the lease commits, the removal does not
    const report = await flush(ME, NOW, vi.fn(async () => ok("applied")));
    expect(report.storageFailed).toBe(true);
    store.failAfter = Infinity;
    const replay = vi.fn(async (s: QueuedScan) => { expect(s.client_scan_id).toBe(entry?.client_scan_id); return ok("applied"); });
    await flush(ME, NOW + 31_000, replay);
    expect(replay).toHaveBeenCalledTimes(1);
    expect(await loadQueue(NOW + 31_000)).toEqual([]);
  });

  it("claim and discard say when nothing could be changed", async () => {
    const legacy = { client_scan_id: "33333333-3333-4333-8333-333333333333", ...SCAN, scanned_at: AT.toISOString(), attempts: 0, next_try_at: 0 };
    window.localStorage.setItem(LEGACY_KEY, JSON.stringify([legacy]));
    store.failCommits = 2;
    expect(await claim([legacy.client_scan_id], ME)).toEqual({ changed: 0, saved: false });
    expect(await discard([legacy.client_scan_id])).toEqual({ changed: 0, saved: false });
    expect(window.localStorage.getItem(LEGACY_KEY)).not.toBeNull(); // nothing moved or lost
    expect((await loadQueue(NOW))[0]?.owner).toBeUndefined();
  });
});


describe("offline scan queue - rollout between page and server versions", () => {
  it.each([
    ["no such route yet (an older server)", 404, "INTERNAL_ERROR"],
    ["method not allowed yet", 405, "INTERNAL_ERROR"],
    ["a schema the server does not know yet", 422, "VALIDATION_ERROR"],
    ["the server asks this page to reload", 503, "CLIENT_UPDATE_REQUIRED"],
  ])("keeps the scan when %s", async (_label, status, code) => {
    const entry = await enqueue(SCAN, ME, AT);
    const report = await flush(ME, NOW, vi.fn(async () => fail(status, code)));
    expect(report.offline).toBe(true);
    expect((await loadQueue(NOW)).map((s) => s.client_scan_id)).toEqual([entry?.client_scan_id]);
  });

  it("still ends a scan on a real domain refusal (an invalid card is a 404 with a code)", async () => {
    await enqueue(SCAN, ME, AT);
    await flush(ME, NOW, vi.fn(async () => fail(404, "INVALID_QR")));
    expect(await loadQueue(NOW)).toEqual([]);
  });

  it("the first queue version keeps its scan on the new server's answer", () => {
    // The first version's decision, exactly (independent audit 2026-10-17): only a
    // network error or a status >= 502 kept a scan; anything else deleted it.
    const firstVersionKeeps = (r: ApiResult<unknown>) => !r.ok && (r.error.code === "NETWORK_ERROR" || r.status >= 502);
    // What the new server answers that page (backend test:
    // test_a_page_of_the_first_queue_version_is_told_to_reload_not_refused).
    expect(firstVersionKeeps(fail(503, "CLIENT_UPDATE_REQUIRED", 60))).toBe(true);
    // ...whereas a 422 - what a plain schema change would have answered - lost it.
    expect(firstVersionKeeps(fail(422, "VALIDATION_ERROR"))).toBe(false);
  });
});

describe("offline scan queue - capacity admits new scans, never evicts saved ones", () => {
  const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  const saved = (n: number, owner: Owner | null = ME, at = AT): QueuedScan => ({
    client_scan_id: uuid(n), ...SCAN, target_id: n, scanned_at: at.toISOString(), attempts: 0, next_try_at: 0,
    ...(owner ? { owner } : {}),
  });
  const range = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i);
  const ids = (list: unknown) => new Set((list as QueuedScan[]).map((s) => s.client_scan_id));
  const idsOf = (ns: number[]) => new Set(ns.map(uuid));
  const setLegacy = (list: unknown[]) => window.localStorage.setItem(LEGACY_KEY, JSON.stringify(list));

  it("a full store plus a first-version entry keeps all 201 when a new scan is refused (audit repro)", async () => {
    store.value = range(0, 200).map((n) => saved(n));
    setLegacy([saved(900, null)]);
    const all = idsOf([...range(0, 200), 900]);
    expect(ids(await loadQueue(NOW))).toEqual(all);
    expect(await enqueue(SCAN, ME, AT)).toBeNull(); // admission refused: the queue is full
    expect(ids(store.value)).toEqual(all); // ...and nothing already saved was dropped
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull(); // removed only now that it is stored
    const state = await queueState(ME, NOW);
    expect(ids(state.quarantined)).toEqual(idsOf([900])); // still nobody's: never filed under ME
    expect(state.mine).toHaveLength(200);
  });

  it("migrates several first-version entries, overlapping ids once, keeping the stored copy", async () => {
    store.value = [...range(0, 198).map((n) => saved(n)), { ...saved(500, null), attempts: 3 }];
    setLegacy([{ ...saved(500, null), attempts: 0 }, saved(501, null), saved(502, null)]);
    expect(await enqueue(SCAN, ME, AT)).toBeNull(); // 198 + 3 = 201 >= 200
    const stored = store.value as QueuedScan[];
    expect(ids(stored)).toEqual(idsOf([...range(0, 198), 500, 501, 502]));
    expect(stored.filter((s) => s.client_scan_id === uuid(500))).toEqual([expect.objectContaining({ attempts: 3 })]);
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it("drains an over-capacity backlog: every scan sent once, then new scans are admitted again", async () => {
    store.value = range(0, 230).map((n) => saved(n));
    expect(await enqueue(SCAN, ME, AT)).toBeNull();
    const sent: string[] = [];
    await flush(ME, NOW, vi.fn(async (s: QueuedScan) => { sent.push(s.client_scan_id); return ok("applied"); }));
    expect(new Set(sent)).toEqual(idsOf(range(0, 230)));
    expect(sent).toHaveLength(230);
    expect(store.value).toEqual([]);
    expect(await enqueue(SCAN, ME, AT)).not.toBeNull();
  });

  it("leases and backs off every entry of a backlog without dropping one", async () => {
    store.value = range(0, 230).map((n) => saved(n));
    const send = vi.fn(async () => fail(0, "NETWORK_ERROR"));
    const report = await flush(ME, NOW, send);
    expect(report.offline).toBe(true);
    expect(send).toHaveBeenCalledTimes(230);
    const stored = store.value as QueuedScan[];
    expect(ids(stored)).toEqual(idsOf(range(0, 230)));
    expect(stored.every((s) => s.attempts === 1 && s.next_try_at > NOW && s.lease_until === 0)).toBe(true);
  });

  it("claims and deletes exactly the named entries of a backlog over capacity", async () => {
    store.value = range(0, 200).map((n) => saved(n));
    setLegacy(range(900, 905).map((n) => saved(n, null)));
    expect(await claim([uuid(900), uuid(901), uuid(5)], ME)).toEqual({ changed: 2, saved: true }); // uuid(5) is ME's already
    expect(ids(store.value)).toEqual(idsOf([...range(0, 200), ...range(900, 905)]));
    const state = await queueState(ME, NOW);
    expect(ids(state.quarantined)).toEqual(idsOf([902, 903, 904]));
    expect(await discard([uuid(903)])).toEqual({ changed: 1, saved: true });
    expect(ids(store.value)).toEqual(idsOf([...range(0, 200), 900, 901, 902, 904]));
  });

  it("keeps expired and another account's entries in a backlog; they are not sent", async () => {
    const old = new Date(AT.getTime() - 25 * 60 * 60 * 1000);
    store.value = [...range(0, 199).map((n) => saved(n)), saved(300, OTHER)];
    setLegacy([saved(901, null, old)]);
    const send = vi.fn(async (s: QueuedScan) => { expect([uuid(300), uuid(901)]).not.toContain(s.client_scan_id); return ok("applied"); });
    await flush(ME, NOW, send);
    expect(send).toHaveBeenCalledTimes(199);
    expect(ids(store.value)).toEqual(idsOf([300, 901]));
    const state = await queueState(ME, NOW);
    expect(ids(state.expired)).toEqual(idsOf([901]));
    expect(ids(state.quarantined)).toEqual(idsOf([300]));
  });

  it("a failed migration write changes nothing and keeps the first-version key", async () => {
    store.value = range(0, 200).map((n) => saved(n));
    const before = structuredClone(store.value);
    setLegacy(range(900, 903).map((n) => saved(n, null)));
    const raw = window.localStorage.getItem(LEGACY_KEY);
    store.failCommits = 1;
    expect(await enqueue(SCAN, ME, AT)).toBeNull();
    expect(store.value).toEqual(before);
    expect(window.localStorage.getItem(LEGACY_KEY)).toBe(raw); // not removed: nothing was migrated
    expect(ids(await loadQueue(NOW))).toEqual(idsOf([...range(0, 200), 900, 901, 902])); // still shown
    expect(await discard([])).toEqual({ changed: 0, saved: true }); // the next write migrates them
    expect(ids(store.value)).toEqual(idsOf([...range(0, 200), 900, 901, 902]));
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it("leaves a first-version key it could not fully read in place", async () => {
    setLegacy([saved(900, null), { client_scan_id: "half-written" }]);
    const raw = window.localStorage.getItem(LEGACY_KEY);
    expect(await enqueue(SCAN, ME, AT)).not.toBeNull();
    expect(ids(store.value).has(uuid(900))).toBe(true); // the readable entry is kept
    expect(window.localStorage.getItem(LEGACY_KEY)).toBe(raw); // the rest is not deleted
  });
});
