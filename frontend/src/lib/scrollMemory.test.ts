import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rememberScroll, restoreScroll } from "./scrollMemory";

let y = 0;
let frames: FrameRequestCallback[] = [];
const flush = (n = 5) => {
  for (let i = 0; i < n; i++) {
    const due = frames;
    frames = [];
    for (const cb of due) cb(0);
  }
};

beforeEach(() => {
  y = 0;
  frames = [];
  window.sessionStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(((_x: number, top: number) => { y = top; }) as typeof window.scrollTo);
  Object.defineProperty(window, "scrollY", { configurable: true, get: () => y });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
});
afterEach(() => vi.restoreAllMocks());

describe("scroll memory", () => {
  it("returns to the remembered position once, then forgets it", () => {
    y = 640;
    rememberScroll("home");
    y = 0;
    const stop = restoreScroll("home");
    flush();
    expect(y).toBe(640);
    stop();
    y = 0;
    restoreScroll("home");
    flush();
    expect(y).toBe(0); // used once: a later visit opens at the top
  });

  it("keeps the position while the page above it is still loading", () => {
    y = 800;
    rememberScroll("home");
    y = 0;
    const stop = restoreScroll("home");
    flush(1);
    y = 300; // the page was still short: the browser could only go this far
    flush(1);
    expect(y).toBe(800);
    stop();
  });

  it("does nothing when nothing was remembered or the visitor was at the top", () => {
    restoreScroll("home");
    flush();
    rememberScroll("home"); // at the top
    restoreScroll("home");
    flush();
    expect(window.scrollTo).not.toHaveBeenCalled();
  });

  it("stops at once when the visitor scrolls themselves", () => {
    y = 900;
    rememberScroll("home");
    y = 0;
    const stop = restoreScroll("home");
    window.dispatchEvent(new Event("wheel"));
    flush();
    expect(y).toBe(0);
    stop();
  });
});
