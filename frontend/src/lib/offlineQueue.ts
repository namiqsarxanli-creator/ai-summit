// A staff phone's durable queue of scans made while the network was down.
//
// Guarantees, and how they are kept:
//
// * No scan is lost to a concurrent write. Every change is ONE IndexedDB
//   readwrite transaction that reads the queue, changes entries BY THEIR
//   client_scan_id only and writes it back. IndexedDB runs overlapping readwrite
//   transactions on one store one after another, across every tab and window of
//   this origin, so two contexts can never interleave a read and a write.
//   (localStorage cannot give that: even under a Web Lock, Chrome lets another
//   tab read a value this tab already replaced - measured: 50 reported, 49
//   kept.) Without IndexedDB the queue is UNSUPPORTED: nothing is queued or
//   changed and the page says so instead of claiming a scan is saved. A
//   per-entry lease also keeps two tabs from sending one scan at the same moment.
// * Entries of the first version of this queue (in localStorage) are moved into
//   IndexedDB by the next change, merged by client_scan_id (never duplicated),
//   and stay quarantined until a person claims or deletes them.
// * A change that could not be written (storage full, blocked) is reported as
//   not done - a scan is never "queued", a lease never "taken", a claim or a
//   deletion never "done" unless the write succeeded.
// * A scan is never paid twice. Each gets its own client id once, at the moment
//   of scanning; the server answers a replay with the stored result.
// * A scan is filed under the account that made it (an opaque label from
//   GET /api/staff/me) and the dataset it belongs to. It is replayed only for
//   that account and dataset - the server refuses anything else as well - and
//   is never silently discarded on sign-out: entries of another account, an
//   older dataset or an older version of this queue are QUARANTINED, kept and
//   shown, until that account signs in again or a person decides explicitly.
// * Transient failures keep the scan: no network, 408, 425, 429 and every 5xx,
//   with backoff (5 s doubling to 5 min, or the server's Retry-After when longer).
//   401 stops the replay and asks for a new sign-in; the queue is kept. Any other
//   answer is final for that entry: accepted, sent for review, or refused.
// * Nothing here shows points: a queued scan has earned nothing until the server
//   answers - and a late one only after a Game Master's review.
// * MAX_QUEUE limits ADMISSION of new scans only. A scan already saved - or
//   migrated from the first version - is never trimmed or evicted: a change may
//   drop an entry only when it names it as removed (answered by the server, or
//   deleted by a person); any other loss aborts the transaction. A backlog over
//   MAX_QUEUE is kept and drains as it is answered; new scans are refused (and
//   said so) until it is below the limit again. An entry older than MAX_AGE_MS
//   is no longer sent automatically - it is shown as expired, and only a person
//   deletes it.
// * Rollout: an answer that says the SERVER does not understand this page yet
//   (no such route: 404/405 without an error envelope; a schema mismatch: 422
//   VALIDATION_ERROR) keeps the scan and retries; a page of the first queue
//   version is answered CLIENT_UPDATE_REQUIRED (503) for the same reason.

import { apiPost, NETWORK_ERROR, type ApiResult } from "@/lib/api";

export type ScanKind = "MASTERCLASS_DOOR" | "PLAYGROUND_COMPLETE" | "MENTOR_COMPLETE";
export type Owner = { queue_owner: string; dataset_generation: number; name?: string };
export type QueuedScan = {
  client_scan_id: string;
  kind: ScanKind;
  target_id: number;
  phase: "ENTRY" | "EXIT" | null;
  card_token: string;
  scanned_at: string;
  attempts: number;
  next_try_at: number;
  lease_until?: number;
  last_status?: number;
  owner?: Owner; // absent on entries from the first version of this queue
};
export type Submitted = {
  id: number;
  client_scan_id: string;
  status: "applied" | "pending_review" | "rejected" | "failed";
  outcome: { award?: { outcome: string; points: number }; name?: string } | null;
};
export type FlushReport = {
  answered: { scan: QueuedScan; result: ApiResult<Submitted> }[];
  authNeeded: boolean;
  offline: boolean;
  // A queue change could not be written: nothing was claimed as done; the scans
  // stay where they are and are tried again (the server answers a replay once).
  storageFailed: boolean;
  // No IndexedDB: the queue is not used at all in this browser.
  unsupported: boolean;
};
export type Changed = { changed: number; saved: boolean };
export type QueueState = { mine: QueuedScan[]; quarantined: QueuedScan[]; expired: QueuedScan[] };

const KEY = "facemark.offline-scans.v1";
export const MAX_QUEUE = 200;
const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const FIRST_RETRY_MS = 5_000;
const MAX_RETRY_MS = 5 * 60_000;
const LEASE_MS = 30_000;

function valid(x: unknown): x is QueuedScan {
  if (typeof x !== "object" || x === null) return false;
  const s = x as Record<string, unknown>;
  return typeof s.client_scan_id === "string" && typeof s.kind === "string" && typeof s.target_id === "number"
    && typeof s.card_token === "string" && typeof s.scanned_at === "string" && typeof s.attempts === "number"
    && typeof s.next_try_at === "number";
}

// Valid entries, whatever their age: an old one is shown as expired, never dropped.
function fresh(list: unknown): QueuedScan[] {
  if (!Array.isArray(list)) return [];
  return list.filter(valid);
}
const expired = (s: QueuedScan, now: number) => now - Date.parse(s.scanned_at) >= MAX_AGE_MS;

// Entries left in localStorage by the first version of this queue (or by a page
// of that version still open somewhere). Read-only here; removed only after they
// were committed to IndexedDB.
function legacyRaw(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}
function legacyEntries(now: number, raw: string | null = legacyRaw()): QueuedScan[] {
  try {
    return raw ? fresh(JSON.parse(raw)) : [];
  } catch {
    return [];
  }
}
// Whether every item of the old key was understood (and so is migrated by
// legacyEntries). Anything else stays in localStorage rather than be deleted.
function legacyFullyReadable(raw: string): boolean {
  try {
    const list: unknown = JSON.parse(raw);
    return Array.isArray(list) && list.every(valid);
  } catch {
    return false;
  }
}

function merge(stored: QueuedScan[], legacy: QueuedScan[]): QueuedScan[] {
  const ids = new Set(stored.map((s) => s.client_scan_id));
  return [...stored, ...legacy.filter((s) => !ids.has(s.client_scan_id))];
}

// One read-modify-write. `done` is true only when the new state was committed.
// `removed` names the entries this change deliberately drops; dropping any other
// entry is a bug that must not reach storage (see mutate).
type Mutation<T> = (queue: QueuedScan[]) => { queue: QueuedScan[]; result: T; removed?: readonly string[] };
type Mutated<T> = { done: true; result: T } | { done: false };
// The storage seam: an ATOMIC read-modify-write of the raw stored value (as one
// IndexedDB readwrite transaction does), and a plain read. Everything the queue
// decides - age, validation, migration of first-version entries - is done above
// it, so every backend behaves the same.
export interface QueueBackend {
  transact<T>(change: (raw: unknown) => { next: unknown; result: T }): Promise<Mutated<T>>;
  snapshot(): Promise<unknown>;
}

const DB_NAME = "facemark-offline";
const STORE = "queue";
const RECORD = "scans";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => { if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("blocked"));
  });
}

export const indexedDbBackend: QueueBackend = {
  async transact<T>(change: (raw: unknown) => { next: unknown; result: T }): Promise<Mutated<T>> {
    const db = await openDb();
    try {
      return await new Promise<Mutated<T>>((resolve) => {
        let result: { done: true; result: T } | null = null;
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const got = store.get(RECORD);
        got.onsuccess = () => {
          try {
            const { next, result: value } = change(got.result);
            store.put(next, RECORD);
            result = { done: true, result: value };
          } catch {
            tx.abort();
          }
        };
        // Only a COMMITTED transaction counts as done.
        tx.oncomplete = () => resolve(result ?? { done: false });
        tx.onerror = () => resolve({ done: false });
        tx.onabort = () => resolve({ done: false });
      });
    } finally {
      db.close();
    }
  },
  async snapshot(): Promise<unknown> {
    const db = await openDb();
    try {
      return await new Promise<unknown>((resolve, reject) => {
        const req = db.transaction(STORE, "readonly").objectStore(STORE).get(RECORD);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    } finally {
      db.close();
    }
  },
};

let override: QueueBackend | null = null;
// Tests replace the storage with one that serialises like IndexedDB does.
export function setQueueBackendForTests(backend: QueueBackend | null): void {
  override = backend;
}
function backend(): QueueBackend | null {
  if (override) return override;
  return typeof indexedDB !== "undefined" && indexedDB !== null ? indexedDbBackend : null;
}

// Whether this browser can keep a queue safely across tabs (see the top).
export function durableQueueSupported(): boolean {
  return backend() !== null;
}

async function mutate<T>(change: Mutation<T>, now = Date.now()): Promise<Mutated<T>> {
  const store = backend();
  if (!store) return { done: false };
  const raw = legacyRaw();
  const legacy = legacyEntries(now, raw);
  let outcome: Mutated<T>;
  try {
    outcome = await store.transact((stored) => {
      const current = merge(fresh(stored), legacy);
      const { queue, result, removed = [] } = change(current);
      // Nothing saved is ever lost on the way: every entry that was there (the
      // migrated ones included) is still there unless this change removed it by
      // name. Throwing aborts the transaction - nothing is written, done: false.
      const kept = new Set(queue.map((s) => s.client_scan_id));
      const named = new Set(removed);
      if (current.some((s) => !kept.has(s.client_scan_id) && !named.has(s.client_scan_id))) {
        throw new Error("a queued scan would be lost");
      }
      return { next: queue, result };
    });
  } catch {
    return { done: false };
  }
  // Committed: every migrated first-version entry is now in the store (or was
  // removed by name). The old key is removed only if all of it was understood and
  // nothing (e.g. a still-open page of the first version) wrote to it meanwhile;
  // otherwise the next change merges it again, by id.
  if (outcome.done && raw !== null && legacyFullyReadable(raw) && legacyRaw() === raw) {
    try { window.localStorage.removeItem(KEY); } catch { /* merged by id next time */ }
  }
  return outcome;
}

async function read(now: number): Promise<QueuedScan[]> {
  const store = backend();
  if (!store) return legacyEntries(now);
  try {
    return merge(fresh(await store.snapshot()), legacyEntries(now));
  } catch {
    return legacyEntries(now);
  }
}

export const sameOwner = (a: Owner | undefined, b: Owner) =>
  a !== undefined && a.queue_owner === b.queue_owner && a.dataset_generation === b.dataset_generation;

export async function queueState(owner: Owner | null, now = Date.now()): Promise<QueueState> {
  const all = await read(now);
  const old = all.filter((s) => expired(s, now));
  const current = all.filter((s) => !expired(s, now));
  if (owner === null) return { mine: [], quarantined: current, expired: old };
  return {
    mine: current.filter((s) => sameOwner(s.owner, owner)),
    quarantined: current.filter((s) => !sameOwner(s.owner, owner)),
    expired: old,
  };
}

export function loadQueue(now = Date.now()): Promise<QueuedScan[]> {
  return read(now);
}

export async function enqueue(
  scan: Pick<QueuedScan, "kind" | "target_id" | "phase" | "card_token">,
  owner: Owner,
  now = new Date(),
): Promise<QueuedScan | null> {
  const entry: QueuedScan = {
    ...scan,
    owner,
    client_scan_id: crypto.randomUUID(),
    scanned_at: now.toISOString(),
    attempts: 0,
    next_try_at: now.getTime(),
  };
  const outcome = await mutate((queue) =>
    queue.length >= MAX_QUEUE ? { queue, result: false } : { queue: [...queue, entry], result: true },
    now.getTime());
  return outcome.done && outcome.result ? entry : null;
}

// A person's explicit decision about quarantined entries.
export async function claim(ids: string[], owner: Owner): Promise<Changed> {
  // Only entries filed under nobody (the first version of this queue) can be
  // claimed: another account's scans are never re-filed as this account's.
  const outcome = await mutate((queue) => {
    let n = 0;
    const next = queue.map((s) => {
      if (!ids.includes(s.client_scan_id) || s.owner !== undefined) return s;
      n += 1;
      return { ...s, owner, next_try_at: 0 };
    });
    return { queue: next, result: n };
  });
  return outcome.done ? { changed: outcome.result, saved: true } : { changed: 0, saved: false };
}
export async function discard(ids: string[]): Promise<Changed> {
  const outcome = await mutate((queue) => {
    const next = queue.filter((s) => !ids.includes(s.client_scan_id));
    return { queue: next, result: queue.length - next.length, removed: ids };
  });
  return outcome.done ? { changed: outcome.result, saved: true } : { changed: 0, saved: false };
}

export const submitOffline = (s: QueuedScan) =>
  apiPost<Submitted>("/api/staff/offline-scans", {
    client_scan_id: s.client_scan_id, kind: s.kind, target_id: s.target_id, phase: s.phase,
    card_token: s.card_token, scanned_at: s.scanned_at,
    queue_owner: s.owner?.queue_owner, dataset_generation: s.owner?.dataset_generation,
  });

// The answer says nothing about the scan itself: keep it and try again later.
export function retryable(r: ApiResult<unknown>): boolean {
  if (r.ok) return false;
  if (r.error.code === NETWORK_ERROR || r.status === 0 || r.status === 408 || r.status === 425
    || r.status === 429 || r.status >= 500) return true;
  // The server does not understand this page yet (a rollout in progress): no
  // such route (no error envelope, so no domain code), or a schema mismatch.
  // Retrying keeps the scan; a domain refusal (INVALID_QR, 403, 409) still ends it.
  return ((r.status === 404 || r.status === 405) && r.error.code === "INTERNAL_ERROR")
    || (r.status === 422 && r.error.code === "VALIDATION_ERROR");
}
// Kept for callers that decide whether to queue a live scan in the first place.
export const unreachable = retryable;

function backoff(attempts: number, retryAfterSeconds?: number): number {
  const wait = Math.min(FIRST_RETRY_MS * 2 ** attempts, MAX_RETRY_MS);
  return retryAfterSeconds === undefined ? wait : Math.max(wait, Math.min(retryAfterSeconds * 1000, 60 * 60_000));
}

// Takes the next due entry of THIS owner and leases it (other tabs skip it).
// `stored: false` - the lease could not be written, so nothing may be sent.
async function lease(owner: Owner, now: number): Promise<{ scan: QueuedScan | null; stored: boolean }> {
  const outcome = await mutate<QueuedScan | null>((queue) => {
    const due = queue.find((s) => sameOwner(s.owner, owner) && !expired(s, now) && s.next_try_at <= now && (s.lease_until ?? 0) <= now);
    if (!due) return { queue, result: null };
    const leased = { ...due, lease_until: now + LEASE_MS };
    return { queue: queue.map((s) => (s.client_scan_id === due.client_scan_id ? leased : s)), result: leased };
  }, now);
  return outcome.done ? { scan: outcome.result, stored: true } : { scan: null, stored: false };
}

export async function flush(owner: Owner, now = Date.now(), send = submitOffline): Promise<FlushReport> {
  const report: FlushReport = { answered: [], authNeeded: false, offline: false, storageFailed: false, unsupported: false };
  if (!durableQueueSupported()) {
    report.unsupported = true;
    return report;
  }
  // Each pass leases a different entry (a leased or backed-off one is not due
  // again at `now`), so this ends after at most one pass per entry - also for a
  // migrated backlog over MAX_QUEUE. `seen` stops a store that does not keep
  // what it reported as written.
  const seen = new Set<string>();
  for (;;) {
    const taken = await lease(owner, now);
    if (!taken.stored) {
      report.storageFailed = true; // no lease written: nothing is sent
      break;
    }
    const scan = taken.scan;
    if (!scan || seen.has(scan.client_scan_id)) break;
    seen.add(scan.client_scan_id);
    const result = await send(scan);
    if (retryable(result)) {
      const retryAfter = !result.ok ? result.retryAfterSeconds : undefined;
      const kept = await mutate((queue) => ({
        queue: queue.map((s) => s.client_scan_id === scan.client_scan_id
          ? { ...s, attempts: s.attempts + 1, next_try_at: now + backoff(s.attempts, retryAfter), lease_until: 0, last_status: result.ok ? undefined : result.status }
          : s),
        result: null,
      }), now);
      report.offline = true;
      if (!kept.done) {
        // The backoff could not be written: the entry keeps its lease and is
        // retried when that expires. Stop rather than spin on a failing store.
        report.storageFailed = true;
        break;
      }
      continue; // the next entry may still go through (e.g. one 500)
    }
    if (!result.ok && result.status === 401) {
      // Signed out: keep everything, release the lease, stop.
      const released = await mutate((queue) => ({ queue: queue.map((s) => s.client_scan_id === scan.client_scan_id ? { ...s, lease_until: 0 } : s), result: null }), now);
      report.authNeeded = true;
      if (!released.done) report.storageFailed = true; // the lease simply expires later
      break;
    }
    const removed = await mutate((queue) => ({
      queue: queue.filter((s) => s.client_scan_id !== scan.client_scan_id), result: null, removed: [scan.client_scan_id],
    }), now);
    report.answered.push({ scan, result });
    if (!removed.done) {
      // Answered, but still stored: it will be sent again after its lease, and the
      // server answers that replay with the stored result - never a second award.
      report.storageFailed = true;
      break;
    }
  }
  return report;
}
