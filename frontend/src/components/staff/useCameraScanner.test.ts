import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCameraScanner } from "./useCameraScanner";

function fakeStream(): MediaStream & { track: { stop: ReturnType<typeof vi.fn> } } {
  const track = { stop: vi.fn() };
  return { getTracks: () => [track], track } as unknown as MediaStream & {
    track: { stop: ReturnType<typeof vi.fn> };
  };
}

const getUserMedia = vi.fn();

beforeEach(() => {
  vi.stubGlobal("navigator", {
    ...globalThis.navigator,
    mediaDevices: { getUserMedia },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  getUserMedia.mockReset();
});

describe("useCameraScanner — permission and availability states", () => {
  it("starts in the starting state before getUserMedia resolves", () => {
    getUserMedia.mockReturnValue(new Promise(() => {})); // never resolves in this test
    const { result } = renderHook(() => useCameraScanner(true, vi.fn()));
    expect(result.current.status).toEqual({ kind: "starting" });
  });

  it("reports denied for NotAllowedError", async () => {
    const err = Object.assign(new Error("no"), { name: "NotAllowedError" });
    getUserMedia.mockRejectedValue(err);
    const { result } = renderHook(() => useCameraScanner(true, vi.fn()));
    await waitFor(() => expect(result.current.status).toEqual({ kind: "denied" }));
  });

  it("reports unavailable for NotFoundError (no camera on the device)", async () => {
    const err = Object.assign(new Error("no"), { name: "NotFoundError" });
    getUserMedia.mockRejectedValue(err);
    const { result } = renderHook(() => useCameraScanner(true, vi.fn()));
    await waitFor(() => expect(result.current.status).toEqual({ kind: "unavailable" }));
  });

  it("reports a generic error for anything else getUserMedia throws", async () => {
    getUserMedia.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useCameraScanner(true, vi.fn()));
    await waitFor(() => expect(result.current.status).toEqual({ kind: "error" }));
  });

  it("never calls getUserMedia while inactive", () => {
    renderHook(() => useCameraScanner(false, vi.fn()));
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});

describe("useCameraScanner — track lifecycle", () => {
  it("stops every track when the hook unmounts after a successful stream", async () => {
    const stream = fakeStream();
    getUserMedia.mockResolvedValue(stream);
    const { result, unmount } = renderHook(() => useCameraScanner(true, vi.fn()));
    await waitFor(() => expect(result.current.status).toEqual({ kind: "streaming" }));

    unmount();

    expect(stream.track.stop).toHaveBeenCalled();
  });

  it("stops the stream if `active` turns false before it settles", async () => {
    const stream = fakeStream();
    let resolveStream: (s: MediaStream) => void = () => {};
    getUserMedia.mockReturnValue(new Promise((resolve) => (resolveStream = resolve)));
    const { rerender } = renderHook(({ active }) => useCameraScanner(active, vi.fn()), {
      initialProps: { active: true },
    });

    rerender({ active: false });
    resolveStream(stream);

    await waitFor(() => expect(stream.track.stop).toHaveBeenCalled());
  });

  it("restarts the camera (a fresh getUserMedia call) when active flips false then true", async () => {
    getUserMedia.mockResolvedValue(fakeStream());
    const { rerender } = renderHook(({ active }) => useCameraScanner(active, vi.fn()), {
      initialProps: { active: true },
    });
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));

    rerender({ active: false });
    rerender({ active: true });

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
  });
});
