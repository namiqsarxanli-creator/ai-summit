// The shared realtime/reconciliation owner (ARCHITECTURE.md §17,
// IMPLEMENTATION_PLAN.md M14). ONE `EventSource` connection, shared by
// every mounted surface — never a separate connection per component —
// coordinating the authoritative score snapshot (Home/ScoreSummary)
// and the leaderboard's top-N view while it is mounted.
//
// SSE is an invalidation channel ONLY (§17.7): no event payload is
// ever treated as authoritative state. Every displayed value always
// comes from a fresh `GET /api/attendees/me/score` or
// `GET /api/leaderboard` — this file never computes `total += points`.
//
// Protocol (§17.4), implemented exactly:
//   1. Subscribe first, then snapshot — `ready` triggers the first
//      fetch; nothing is fetched before it.
//   2. Any signal -> refetch.
//   3. Reconnect -> refetch (the server sends a fresh `ready` on every
//      new connection, which already triggers this — no separate
//      reconnect-specific fetch exists here).
//   4. Visibility regained -> refetch immediately.
//   5. Every 20s even while connected -> refetch (the correctness
//      backstop — this is what makes a fully lost signal self-heal).
//   6. No frame of any kind for 45s -> treat the stream as dead,
//      close, reopen, refetch.

import { useSyncExternalStore } from "react";

import { onDatasetGeneration } from "@/lib/api";
import {
  fetchLeaderboardTop,
  type LeaderboardEntry,
  type LeaderboardResponse,
} from "@/lib/leaderboard";
import { fetchMyScore, type ScoreSnapshot } from "@/lib/score";

const RECONCILE_INTERVAL_MS = 20_000;
const DEAD_CONNECTION_MS = 45_000;
const FALLBACK_AFTER_FAILURES = 3;
const FALLBACK_POLL_MS = 5_000;
const FALLBACK_SSE_RETRY_MS = 60_000;

export type ScoreStoreState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "error"; code: string }
  | { status: "ok"; score: ScoreSnapshot; justAwardedPoints: number | null };

export type LeaderboardStoreState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "error"; code: string }
  | { status: "ok"; leaderboard: LeaderboardResponse };

type Listener = () => void;

function isSignedOutCode(code: string): boolean {
  return code === "UNAUTHENTICATED" || code === "REGISTRATION_NOT_VERIFIED";
}

// M13's LeaderboardView owns "show all" pagination itself (its own
// component state) — this store only ever refreshes the DEFAULT top-N
// view, exactly the piece §17.4's reconciliation targets ("another
// attendee scoring can change the current attendee's rank"). Paging
// state is untouched by any refetch here.
class RealtimeStore {
  private eventSource: EventSource | null = null;
  private scoreListeners = new Set<Listener>();
  private leaderboardListeners = new Set<Listener>();
  private scoreState: ScoreStoreState = { status: "loading" };
  private leaderboardState: LeaderboardStoreState = { status: "loading" };

  private animatedAwardIds = new Set<number>();
  private seenFirstScoreSnapshot = false;
  private pageOpenedAt = new Date();
  private justAwardedTimer: ReturnType<typeof setTimeout> | null = null;

  private datasetGeneration: number | null = null;
  private unsubscribeGeneration: (() => void) | null = null;

  /** Guards against a stale response overwriting a newer one: each
   * refetch captures the post-increment sequence number, and only
   * applies its result if it is still the most recently ISSUED
   * request when the response arrives. An older request that resolves
   * after a newer one (any reordering — slow network, retry, whatever)
   * is silently discarded rather than overwriting the newer state.
   * `hardReinitialise` also bumps both, so a request in flight before
   * a reset can never restore pre-reset state after the fact. */
  private scoreRequestSeq = 0;
  private leaderboardRequestSeq = 0;

  private reconcileTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setTimeout> | null = null;
  private consecutiveFailures = 0;
  private fallbackPollTimer: ReturnType<typeof setInterval> | null = null;
  private fallbackRetryTimer: ReturnType<typeof setInterval> | null = null;
  private visibilityInstalled = false;
  /** True once the CURRENT connection has delivered its `ready` event.
   * A newly-mounted leaderboard subscriber fetches immediately only
   * when this is already true (a warm connection it is simply late
   * to) — on a cold connection it waits for `ready`, same as score,
   * so "subscribe first, then snapshot" holds for both. */
  private readySeen = false;

  /** Test-only: this store is a module-level singleton, so tests must
   * explicitly return it to a clean slate between cases rather than
   * re-importing a fresh module instance. Never called by app code. */
  resetForTests(): void {
    this.closeStream();
    this.stopReconciliation();
    this.stopFallback();
    if (this.deadTimer) clearTimeout(this.deadTimer);
    if (this.justAwardedTimer) clearTimeout(this.justAwardedTimer);
    if (this.unsubscribeGeneration) this.unsubscribeGeneration();
    this.scoreListeners = new Set();
    this.leaderboardListeners = new Set();
    this.scoreState = { status: "loading" };
    this.leaderboardState = { status: "loading" };
    this.animatedAwardIds = new Set();
    this.seenFirstScoreSnapshot = false;
    this.pageOpenedAt = new Date();
    this.justAwardedTimer = null;
    this.datasetGeneration = null;
    this.unsubscribeGeneration = null;
    this.deadTimer = null;
    this.consecutiveFailures = 0;
    this.visibilityInstalled = false;
    this.readySeen = false;
    this.scoreRequestSeq = 0;
    this.leaderboardRequestSeq = 0;
  }

  // --- subscription API ---------------------------------------------------

  subscribeScore = (listener: Listener): (() => void) => {
    this.scoreListeners.add(listener);
    this.ensureRunning();
    return () => {
      this.scoreListeners.delete(listener);
      this.stopIfUnused();
    };
  };

  subscribeLeaderboard = (listener: Listener): (() => void) => {
    const wasEmpty = this.leaderboardListeners.size === 0;
    this.leaderboardListeners.add(listener);
    this.ensureRunning();
    // Only an immediate fetch on a WARM connection (one that already
    // delivered `ready`) — mounting late on an already-subscribed
    // stream. On a cold connection, the upcoming `ready` event's own
    // handler fetches, preserving subscribe-first-then-snapshot.
    if (wasEmpty && this.readySeen) void this.refetchLeaderboard();
    return () => {
      this.leaderboardListeners.delete(listener);
      if (this.leaderboardListeners.size === 0) {
        this.leaderboardState = { status: "loading" };
      }
      this.stopIfUnused();
    };
  };

  getScoreSnapshot = (): ScoreStoreState => this.scoreState;
  getLeaderboardSnapshot = (): LeaderboardStoreState => this.leaderboardState;

  // --- lifecycle -----------------------------------------------------------

  private ensureRunning(): void {
    if (this.eventSource || this.fallbackPollTimer) return;
    this.consecutiveFailures = 0;
    this.openStream();
    this.startReconciliation();
    this.installVisibilityListener();
    if (this.unsubscribeGeneration === null) {
      this.unsubscribeGeneration = onDatasetGeneration((generation) =>
        this.handleGeneration(generation),
      );
    }
  }

  private stopIfUnused(): void {
    if (this.scoreListeners.size > 0 || this.leaderboardListeners.size > 0) return;
    this.closeStream();
    this.stopReconciliation();
    this.stopFallback();
    if (this.unsubscribeGeneration) {
      this.unsubscribeGeneration();
      this.unsubscribeGeneration = null;
    }
  }

  private openStream(): void {
    const es = new EventSource("/api/realtime/stream");
    this.eventSource = es;
    this.resetDeadTimer();

    es.addEventListener("ready", (e) => {
      this.resetDeadTimer();
      this.consecutiveFailures = 0;
      this.readySeen = true;
      this.stopFallback();
      const data = _parse(e);
      if (typeof data.dataset_generation === "number") {
        this.handleGeneration(data.dataset_generation);
      }
      // Subscribe-first-then-snapshot: `ready` is what triggers the
      // FIRST fetch, never anything before it.
      if (this.scoreListeners.size > 0) void this.refetchScore();
      if (this.leaderboardListeners.size > 0) void this.refetchLeaderboard();
    });

    es.addEventListener("state_changed", () => {
      this.resetDeadTimer();
      if (this.scoreListeners.size > 0) void this.refetchScore();
    });

    es.addEventListener("leaderboard_changed", () => {
      this.resetDeadTimer();
      if (this.leaderboardListeners.size > 0) void this.refetchLeaderboard();
    });

    es.addEventListener("heartbeat", () => {
      this.resetDeadTimer();
    });

    es.addEventListener("session_invalid", () => {
      this.resetDeadTimer();
      this.closeStream();
      this.stopReconciliation();
      this.stopFallback();
      this.setScoreState({ status: "signed-out" });
      this.setLeaderboardState({ status: "signed-out" });
    });

    es.addEventListener("dataset_reset", (e) => {
      const data = _parse(e);
      this.hardReinitialise(
        typeof data.dataset_generation === "number" ? data.dataset_generation : null,
      );
    });

    es.onerror = () => {
      this.consecutiveFailures += 1;
      if (this.consecutiveFailures >= FALLBACK_AFTER_FAILURES) {
        this.enterFallback();
      }
      // Native EventSource retries the connection itself; when it
      // succeeds, the server's fresh `ready` event drives the refetch
      // (see the `ready` handler above) — no separate reconnect path.
    };
  }

  private closeStream(): void {
    this.eventSource?.close();
    this.eventSource = null;
    this.readySeen = false;
    if (this.deadTimer) {
      clearTimeout(this.deadTimer);
      this.deadTimer = null;
    }
  }

  private resetDeadTimer(): void {
    if (this.deadTimer) clearTimeout(this.deadTimer);
    this.deadTimer = setTimeout(() => {
      // §17.9: no frame of any kind for 45s -> treat as dead, close,
      // reconnect, refetch. A stalled/buffered connection looks
      // identical to a healthy idle one until this fires.
      this.closeStream();
      this.openStream();
    }, DEAD_CONNECTION_MS);
  }

  // --- fallback polling (§17.11) -------------------------------------------

  private enterFallback(): void {
    if (this.fallbackPollTimer) return;
    this.closeStream();
    this.fallbackPollTimer = setInterval(() => {
      if (this.scoreListeners.size > 0) void this.refetchScore();
      if (this.leaderboardListeners.size > 0) void this.refetchLeaderboard();
    }, FALLBACK_POLL_MS);
    this.fallbackRetryTimer = setInterval(() => {
      this.consecutiveFailures = 0;
      this.openStream();
    }, FALLBACK_SSE_RETRY_MS);
  }

  private stopFallback(): void {
    if (this.fallbackPollTimer) {
      clearInterval(this.fallbackPollTimer);
      this.fallbackPollTimer = null;
    }
    if (this.fallbackRetryTimer) {
      clearInterval(this.fallbackRetryTimer);
      this.fallbackRetryTimer = null;
    }
  }

  // --- periodic reconciliation (§17.4 step 5 / invariant #27) -------------

  private startReconciliation(): void {
    if (this.reconcileTimer) return;
    this.reconcileTimer = setInterval(() => {
      if (this.scoreListeners.size > 0) void this.refetchScore();
      if (this.leaderboardListeners.size > 0) void this.refetchLeaderboard();
    }, RECONCILE_INTERVAL_MS);
  }

  private stopReconciliation(): void {
    if (this.reconcileTimer) {
      clearInterval(this.reconcileTimer);
      this.reconcileTimer = null;
    }
  }

  private installVisibilityListener(): void {
    if (this.visibilityInstalled || typeof document === "undefined") return;
    this.visibilityInstalled = true;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        if (this.scoreListeners.size > 0) void this.refetchScore();
        if (this.leaderboardListeners.size > 0) void this.refetchLeaderboard();
      }
    });
  }

  // --- fetching --------------------------------------------------------------

  private async refetchScore(): Promise<void> {
    const seq = ++this.scoreRequestSeq;
    const result = await fetchMyScore();
    // A newer request was issued while this one was in flight — its
    // response (whichever arrives) is authoritative instead. Discard
    // this one unconditionally, even if it happens to be "ok".
    if (seq !== this.scoreRequestSeq) return;
    if (!result.ok) {
      this.setScoreState(
        isSignedOutCode(result.error.code)
          ? { status: "signed-out" }
          : { status: "error", code: result.error.code },
      );
      return;
    }
    this.applyScoreSnapshot(result.data);
  }

  private async refetchLeaderboard(): Promise<void> {
    const seq = ++this.leaderboardRequestSeq;
    const result = await fetchLeaderboardTop();
    if (seq !== this.leaderboardRequestSeq) return;
    if (!result.ok) {
      this.setLeaderboardState(
        isSignedOutCode(result.error.code)
          ? { status: "signed-out" }
          : { status: "error", code: result.error.code },
      );
      return;
    }
    this.setLeaderboardState({ status: "ok", leaderboard: result.data });
  }

  // --- animation (§17.6) -----------------------------------------------------

  private applyScoreSnapshot(snapshot: ScoreSnapshot): void {
    let justAwardedPoints: number | null = null;

    if (!this.seenFirstScoreSnapshot) {
      // "Seeded from the FIRST snapshot" — nothing already reflected
      // on arrival is ever animated.
      for (const award of snapshot.recent_awards) this.animatedAwardIds.add(award.id);
      this.seenFirstScoreSnapshot = true;
    } else {
      const visible = typeof document === "undefined" || document.visibilityState === "visible";
      for (const award of snapshot.recent_awards) {
        if (this.animatedAwardIds.has(award.id)) continue;
        const openedBeforeAward = new Date(award.created_at) >= this.pageOpenedAt;
        if (visible && openedBeforeAward && justAwardedPoints === null) {
          justAwardedPoints = award.points;
        }
        this.animatedAwardIds.add(award.id);
      }
    }

    // Displayed state is always ASSIGNED from the snapshot — never
    // `total += points`.
    this.setScoreState({ status: "ok", score: snapshot, justAwardedPoints });

    if (justAwardedPoints !== null) {
      if (this.justAwardedTimer) clearTimeout(this.justAwardedTimer);
      this.justAwardedTimer = setTimeout(() => {
        if (this.scoreState.status === "ok") {
          this.setScoreState({ ...this.scoreState, justAwardedPoints: null });
        }
      }, 4_000);
    }
  }

  // --- dataset generation (§19.3) ---------------------------------------------

  private handleGeneration(generation: number): void {
    if (this.datasetGeneration === null) {
      this.datasetGeneration = generation;
      return;
    }
    if (generation !== this.datasetGeneration) {
      this.hardReinitialise(generation);
    }
  }

  private hardReinitialise(newGeneration: number | null): void {
    // §19.3: "clears all in-memory state (including `animatedAwardIds`),
    // drops its session, closes the stream, and returns to the entry
    // screen." M15 (reset) is not implemented yet, so there is no
    // live trigger for this in the current build — this is the
    // mechanism M15 will drive; a revoked/expired session already
    // reaches signed-out via `session_invalid` on its own.
    this.animatedAwardIds.clear();
    this.seenFirstScoreSnapshot = false;
    this.datasetGeneration = newGeneration;
    // Invalidate any request already in flight — it must not be able
    // to land after this reset and resurrect pre-reset state.
    this.scoreRequestSeq += 1;
    this.leaderboardRequestSeq += 1;
    this.closeStream();
    this.stopReconciliation();
    this.stopFallback();
    this.setScoreState({ status: "loading" });
    this.setLeaderboardState({ status: "loading" });
    if (this.scoreListeners.size > 0 || this.leaderboardListeners.size > 0) {
      this.ensureRunning();
    }
  }

  // --- state setters ------------------------------------------------------

  private setScoreState(next: ScoreStoreState): void {
    this.scoreState = next;
    for (const listener of this.scoreListeners) listener();
  }

  private setLeaderboardState(next: LeaderboardStoreState): void {
    this.leaderboardState = next;
    for (const listener of this.leaderboardListeners) listener();
  }
}

function _parse(e: Event): Record<string, unknown> {
  const raw = (e as MessageEvent).data;
  if (typeof raw !== "string") return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export const realtimeStore = new RealtimeStore();
export type { LeaderboardEntry };

// `useSyncExternalStore` is what makes ONE shared connection safe to
// read from many components at once: every subscriber re-renders from
// the SAME store snapshot, so two mounted surfaces never independently
// race their own fetch of the same data.
//
// A `getServerSnapshot` is required so Next.js can produce SSR/build
// output for these pages: `EventSource` and `fetch`-from-a-browser
// don't exist server-side, so the server snapshot is always "loading"
// — identical to the client's own initial state before its first
// real snapshot arrives, so hydration never mismatches.
const LOADING_SCORE: ScoreStoreState = { status: "loading" };
const LOADING_LEADERBOARD: LeaderboardStoreState = { status: "loading" };

export function useScoreStore(): ScoreStoreState {
  return useSyncExternalStore(
    realtimeStore.subscribeScore,
    realtimeStore.getScoreSnapshot,
    () => LOADING_SCORE,
  );
}

export function useLeaderboardStore(): LeaderboardStoreState {
  return useSyncExternalStore(
    realtimeStore.subscribeLeaderboard,
    realtimeStore.getLeaderboardSnapshot,
    () => LOADING_LEADERBOARD,
  );
}
