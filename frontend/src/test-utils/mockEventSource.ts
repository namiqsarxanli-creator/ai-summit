// jsdom has no built-in EventSource. This is a controllable fake that
// matches the API surface `lib/realtime.ts` actually uses
// (`addEventListener`, `close`, `onerror`) and lets a test dispatch
// named server events (`ready`, `state_changed`, ...) directly, or
// simulate a connection failure via `.error()`.

import { vi } from "vitest";

type Listener = (event: MessageEvent) => void;

export class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  closed = false;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  private listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)?.add(listener);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
  }

  /** Dispatches a named server event with a JSON-serialisable payload. */
  emit(type: string, data: unknown): void {
    const event = { data: JSON.stringify(data) } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  /** Simulates a connection failure (native EventSource `onerror`). */
  error(): void {
    this.onerror?.();
  }

  static reset(): void {
    MockEventSource.instances = [];
  }

  static latest(): MockEventSource {
    const instance = MockEventSource.instances.at(-1);
    if (!instance) throw new Error("no MockEventSource instance has been created yet");
    return instance;
  }
}

export function installMockEventSource(): void {
  MockEventSource.reset();
  vi.stubGlobal("EventSource", MockEventSource);
}
