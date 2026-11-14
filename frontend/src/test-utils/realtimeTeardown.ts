// Teardown for tests that mount realtime subscribers, in the only safe order:
//
// 1. unmount every rendered component (their subscriptions end, so nothing can
//    re-subscribe and open a stream later);
// 2. stop the shared store's own background work (stream, timers, listeners);
// 3. only then remove the global mocks (EventSource, fetch) they relied on.
//
// Testing Library's automatic cleanup runs AFTER a test file's own afterEach, so
// a file that unstubbed globals in afterEach left mounted components alive
// without the EventSource mock: a subscription effect flushed late then reached
// `new EventSource` with no EventSource defined (seen only in full-suite runs).
import { cleanup } from "@testing-library/react";
import { vi } from "vitest";

import { realtimeStore } from "@/lib/realtime";

export function teardownRealtime(): void {
  cleanup();
  realtimeStore.resetForTests();
  vi.unstubAllGlobals();
}
