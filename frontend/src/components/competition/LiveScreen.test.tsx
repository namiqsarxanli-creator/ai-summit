import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatCountdown, remainingMs } from "@/lib/screen";

import { LiveScreen, PHOTO_REFRESH_MS, REFRESH_MS } from "./LiveScreen";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const live = (over: object) => ({
  state: "open",
  missing: [],
  server_time: "2026-11-13T12:00:00+00:00", // 16:00 in Baku
  timezone: "Asia/Baku",
  closes_at: "2026-11-13T13:30:00+00:00", // 17:30 in Baku
  top: [
    { place: 1, name: "Anar N.", points: 40, photo_url: null },
    { place: 2, name: "Aysel M.", points: 38, photo_url: null },
  ],
  recent: [{ name: "Aysel M.", points: 12, category: "MASTERCLASS", at: "2026-11-13T11:58:00+00:00" }],
  photos: [],
  ...over,
});

describe("LiveScreen", () => {
  it("shows the live top 10, the latest points and a countdown by the server's clock", async () => {
    // This device's clock is two hours off; the countdown still follows the server.
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    vi.setSystemTime(new Date("2026-11-13T10:00:00Z"));
    vi.stubGlobal("fetch", vi.fn(async () => json(200, live({}))));
    render(<LiveScreen />);
    await act(async () => {});
    const top = within(screen.getByRole("list", { name: "Live top 10" }));
    expect(top.getByText("Anar N.")).toBeInTheDocument();
    expect(top.getByText("40 pts")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Latest points" })).getByText(/Masterclasses/)).toBeInTheDocument();
    expect(screen.getByTestId("countdown")).toHaveTextContent("1:30:00");
    await act(async () => { vi.advanceTimersByTime(1_000); });
    expect(screen.getByTestId("countdown")).toHaveTextContent("1:29:59");
  });

  it("says plainly what is missing instead of looking like a running competition", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, live({ state: "not_configured", missing: ["close"], closes_at: null, top: [], recent: [] }))));
    render(<LiveScreen />);
    expect(await screen.findByText("The competition programme is not set up yet.")).toBeInTheDocument();
    expect(screen.getByText("The competition close time (17:30)")).toBeInTheDocument();
    expect(screen.queryByTestId("countdown")).toBeNull();
    expect(screen.queryByRole("list", { name: "Live top 10" })).toBeNull();
  });

  it("after the close shows the review, without a ranking", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, live({ state: "review", top: [], recent: [] }))));
    render(<LiveScreen />);
    expect(await screen.findByText("The ranking has closed. The Game Master is reviewing the results.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Live top 10" })).toBeNull();
  });

  it("switches to the published final", async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.startsWith("/api/final")
        ? json(200, { status: "published", closes_at: null, version: 1, published_at: null, top: [{ place: 1, name: "Anar N.", points: 40, photo_url: null }], founders: [], founder_rule_confirmed: true })
        : json(200, live({ state: "published", top: [], recent: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<LiveScreen />);
    expect(await screen.findByText("Final ranking")).toBeInTheDocument();
    expect(screen.getByTestId("final-1")).toHaveTextContent("Anar N.");
  });

  it("rotates approved photos and stops showing one removed by the next feed", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    const a = `/api/media/pair/${"a".repeat(32)}-384.jpg`;
    const b = `/api/media/pair/${"b".repeat(32)}-384.jpg`;
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => json(200, live({
      photos: (++calls === 1 ? [a, b] : [a]).map((photo_url) => ({ photo_url })),
    }))));
    render(<LiveScreen />);
    await act(async () => {});
    expect(screen.getByTestId("live-photo")).toHaveAttribute("src", a);
    await act(async () => { vi.advanceTimersByTime(PHOTO_REFRESH_MS); });
    expect(screen.getByTestId("live-photo")).toHaveAttribute("src", b);
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS - PHOTO_REFRESH_MS); });
    expect(screen.getByTestId("live-photo")).toHaveAttribute("src", a);
    expect(screen.getAllByTestId("live-photo")).toHaveLength(1);
  });

  it("does not overlap requests when visibility changes during an in-flight refresh", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    let resolve!: (response: Response) => void;
    const pending = new Promise<Response>((done) => { resolve = done; });
    const fetchMock = vi.fn().mockReturnValueOnce(pending).mockImplementation(async () => json(200, live({})));
    vi.stubGlobal("fetch", fetchMock);
    render(<LiveScreen />);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(json(200, live({}))); });
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the last data when the connection drops, says so, and recovers", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 2) throw new TypeError("network down");
      return json(200, live({}));
    }));
    render(<LiveScreen />);
    await act(async () => {});
    expect(screen.getByText("Anar N.")).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS); });
    expect(screen.getByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.getByText("Anar N.")).toBeInTheDocument(); // the last data stays
    await act(async () => { vi.advanceTimersByTime(5_000); });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(call).toBe(3);
  });
});

describe("countdown helpers", () => {
  it("counts by the server's offset and never below zero", () => {
    const close = "2026-11-13T13:30:00Z";
    expect(remainingMs(close, 0, Date.parse("2026-11-13T13:29:00Z"))).toBe(60_000);
    expect(remainingMs(close, 60_000, Date.parse("2026-11-13T13:29:00Z"))).toBe(0);
    expect(formatCountdown(61_000)).toBe("01:01");
    expect(formatCountdown(3_600_000)).toBe("1:00:00");
    expect(formatCountdown(0)).toBe("00:00");
  });
});
