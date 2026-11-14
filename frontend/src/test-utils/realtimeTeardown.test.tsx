import { render } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { realtimeStore } from "@/lib/realtime";

import { installMockEventSource, MockEventSource } from "./mockEventSource";
import { teardownRealtime } from "./realtimeTeardown";

// A subscriber whose subscription starts one task AFTER mounting - like a
// component that subscribes once an earlier fetch resolves.
function LateSubscriber() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setReady(true), 0);
    return () => clearTimeout(t);
  }, []);
  useEffect(() => (ready ? realtimeStore.subscribeScore(() => {}) : undefined), [ready]);
  return null;
}

afterEach(() => vi.unstubAllGlobals());

describe("teardownRealtime", () => {
  it("unmounts before removing the EventSource mock, so a late subscription never runs", async () => {
    installMockEventSource();
    render(<LateSubscriber />);
    teardownRealtime(); // the test ends before the late subscription happened
    expect(typeof globalThis.EventSource).toBe("undefined"); // the mock is gone ...
    await new Promise((r) => setTimeout(r, 20)); // ... and the late effect never ran
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("closes a stream that was open and leaves no subscriber behind", async () => {
    installMockEventSource();
    render(<LateSubscriber />);
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const stream = MockEventSource.latest();
    teardownRealtime();
    expect(stream.closed).toBe(true);
    expect(realtimeStore.getScoreSnapshot()).toEqual({ status: "loading" });
  });
});
