import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Scanner } from "./Scanner";
import type { CameraStatus } from "./useCameraScanner";
import { useCameraScanner } from "./useCameraScanner";

vi.mock("./useCameraScanner", () => ({ useCameraScanner: vi.fn() }));

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const BOOTH_STAFF_ME = { full_name: "Noah Bergström", role: "BOOTH_STAFF", booth_name: "AIWorks" };
const ADMIN_ME = { full_name: "Jordan Blake", role: "ADMIN", booth_name: null };
const CARD_URL = "https://summit.example/c/GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";
const IDENTITY_A = {
  first_name: "Marcus",
  last_name: "Lindqvist",
  profession: "Data Platform Engineer",
  scan_ref: "bodyA.sigA",
};
const IDENTITY_B = {
  first_name: "Elena",
  last_name: "Petrova",
  profession: "Product Manager",
  scan_ref: "bodyB.sigB",
};
const PRESETS = [
  { points: 7, label: "Custom Seven" },
  { points: 13, label: "Custom Thirteen" },
];

let latestOnDetected: (text: string) => void = () => {};
let cameraStatus: CameraStatus = { kind: "streaming" };
const restartSpy = vi.fn();

function mockUseCameraScanner() {
  vi.mocked(useCameraScanner).mockImplementation((_active, onDetected) => {
    latestOnDetected = onDetected;
    return {
      videoRef: { current: null },
      canvasRef: { current: null },
      status: cameraStatus,
      restart: restartSpy,
    } as unknown as ReturnType<typeof useCameraScanner>;
  });
}

function fetchRouter(overrides: Record<string, () => Response>) {
  return vi.fn(async (url: string) => {
    if (url in overrides) return overrides[url]!();
    throw new Error(`unexpected fetch to ${url}`);
  });
}

beforeEach(() => {
  cameraStatus = { kind: "streaming" };
  restartSpy.mockClear();
  mockUseCameraScanner();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Scanner — confirmation cannot be bypassed", () => {
  it("shows the mandatory Attendee found / Cancel / Confirm screen before anything else, never the award shell directly", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
      }),
    );
    render(<Scanner />);
    await screen.findByText(/Point the camera/);

    await act(async () => {
      latestOnDetected(CARD_URL);
    });

    expect(await screen.findByText("Attendee found")).toBeInTheDocument();
    expect(screen.getByText("Marcus Lindqvist")).toBeInTheDocument();
    expect(screen.getByText("Is this the correct attendee?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();

    // The award/preset shell must not be reachable without clicking Confirm.
    expect(screen.queryByLabelText("Point presets")).not.toBeInTheDocument();
  });
});

describe("Scanner — preset buttons come from the API, never hardcoded", () => {
  it("fetches presets only after Confirm, and renders exactly what the server returned", async () => {
    const fetchMock = fetchRouter({
      "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
      "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
      "/api/staff/points/presets": () => jsonResponse(200, PRESETS),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");

    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/points/presets")).toBe(false);

    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });

    expect(await screen.findByText("+7 · Custom Seven")).toBeInTheDocument();
    expect(screen.getByText("+13 · Custom Thirteen")).toBeInTheDocument();
    // No hardcoded +5/+10/+15 — only whatever the server returned.
    expect(screen.queryByText(/\+5\b/)).not.toBeInTheDocument();
  });

  it("shows admins cannot award, without ever calling the presets endpoint", async () => {
    const fetchMock = fetchRouter({
      "/api/staff/me": () => jsonResponse(200, ADMIN_ME),
      "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");
    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });
    expect(await screen.findByText(/Admins can.t award points/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/points/presets")).toBe(false);
  });
});

describe("Scanner — M11 award flow", () => {
  const AWARD_RESULT = {
    points_log_id: 481,
    points_awarded: 7,
    resulting_total: 45,
    attendee_name: "Marcus L.",
    booth_name: "AIWorks",
    awarded_at: "2026-09-22T17:04:11Z",
    replayed: false,
  };

  async function reachAwardingShell(
    fetchMock: ReturnType<typeof vi.fn>,
  ): Promise<void> {
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");
    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });
    await screen.findByText("+7 · Custom Seven");
  }

  it("clicking a preset awards it and shows the server-derived total, never computed here", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/points/presets") return jsonResponse(200, PRESETS);
      if (url === "/api/staff/points/award") {
        const body = JSON.parse(init?.body as string);
        expect(body).toEqual({
          card_token: "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE",
          points: 7,
          idempotency_key: body.idempotency_key,
          scan_ref: "bodyA.sigA",
        });
        expect(body.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
        return jsonResponse(200, AWARD_RESULT);
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachAwardingShell(fetchMock);

    await act(async () => {
      screen.getByRole("button", { name: "+7 · Custom Seven" }).click();
    });

    expect(await screen.findByText("+7")).toBeInTheDocument();
    expect(screen.getByText("New total: 45")).toBeInTheDocument();
    expect(screen.getByText(/AIWorks/)).toBeInTheDocument();
  });

  it("the idempotency key sent to award is the SAME one generated when confirm opened", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    let sentKey: string | undefined;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/points/presets") return jsonResponse(200, PRESETS);
      if (url === "/api/staff/points/award") {
        sentKey = JSON.parse(init?.body as string).idempotency_key;
        return jsonResponse(200, AWARD_RESULT);
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachAwardingShell(fetchMock);
    expect(randomUUID).toHaveBeenCalledTimes(1);
    const generatedKey = randomUUID.mock.results[0]?.value;

    await act(async () => {
      screen.getByRole("button", { name: "+7 · Custom Seven" }).click();
    });
    await screen.findByText("New total: 45");

    expect(sentKey).toBe(generatedKey);
    expect(randomUUID).toHaveBeenCalledTimes(1); // still not regenerated by the click
    randomUUID.mockRestore();
  });

  it("a second rapid click on a different preset does not send a second award request", async () => {
    let awardCalls = 0;
    let resolveAward: (r: Response) => void = () => {};
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/points/presets") return jsonResponse(200, PRESETS);
      if (url === "/api/staff/points/award") {
        awardCalls += 1;
        return new Promise<Response>((resolve) => {
          resolveAward = resolve;
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachAwardingShell(fetchMock);

    await act(async () => {
      screen.getByRole("button", { name: "+7 · Custom Seven" }).click();
    });
    // The preset shell is gone (state left "awarding"), so a second
    // preset can't physically be clicked — but exercise the guard
    // function directly-equivalent path: the pending screen renders
    // instead of the buttons.
    expect(screen.queryByRole("button", { name: "+13 · Custom Thirteen" })).not.toBeInTheDocument();
    expect(awardCalls).toBe(1);

    await act(async () => {
      resolveAward(jsonResponse(200, AWARD_RESULT));
    });
    await screen.findByText("New total: 45");
    expect(awardCalls).toBe(1);
  });

  it("shows an error and offers rescan when the award is rejected (e.g. DUPLICATE_TASK_REWARD)", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/points/presets") return jsonResponse(200, PRESETS);
      if (url === "/api/staff/points/award") return errorResponse(409, "DUPLICATE_TASK_REWARD");
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachAwardingShell(fetchMock);

    await act(async () => {
      screen.getByRole("button", { name: "+7 · Custom Seven" }).click();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This attendee has already earned points at this booth.",
    );
    expect(screen.getByRole("button", { name: "Scan another attendee" })).toBeInTheDocument();
  });

  it("replayed awards are labelled distinctly from a fresh award", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/points/presets") return jsonResponse(200, PRESETS);
      if (url === "/api/staff/points/award") {
        return jsonResponse(200, { ...AWARD_RESULT, replayed: true });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachAwardingShell(fetchMock);
    await act(async () => {
      screen.getByRole("button", { name: "+7 · Custom Seven" }).click();
    });
    expect(await screen.findByText("Already awarded")).toBeInTheDocument();
  });
});

describe("Scanner — manual entry only reachable after a camera failure", () => {
  it("offers no manual-entry link while the camera is streaming", async () => {
    cameraStatus = { kind: "streaming" };
    vi.stubGlobal("fetch", fetchRouter({ "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME) }));
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Enter code manually" })).not.toBeInTheDocument();
  });

  it("offers no manual-entry link while the camera is merely starting", async () => {
    cameraStatus = { kind: "starting" };
    vi.stubGlobal("fetch", fetchRouter({ "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME) }));
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Enter code manually" })).not.toBeInTheDocument();
  });

  it("offers manual entry once the camera permission is denied", async () => {
    cameraStatus = { kind: "denied" };
    vi.stubGlobal("fetch", fetchRouter({ "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME) }));
    render(<Scanner />);
    const manualButton = await screen.findByRole("button", { name: "Enter code manually" });

    await act(async () => {
      manualButton.click();
    });
    expect(screen.getByLabelText("Card code")).toBeInTheDocument();
  });

  it("offers manual entry when no camera is available", async () => {
    cameraStatus = { kind: "unavailable" };
    vi.stubGlobal("fetch", fetchRouter({ "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME) }));
    render(<Scanner />);
    expect(await screen.findByRole("button", { name: "Enter code manually" })).toBeInTheDocument();
  });
});

describe("Scanner — manual entry actually resolves", () => {
  it("submitting the manual-entry form reaches the same confirmation screen as a camera decode", async () => {
    cameraStatus = { kind: "denied" };
    const fetchMock = fetchRouter({
      "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
      "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);

    const manualButton = await screen.findByRole("button", { name: "Enter code manually" });
    await act(async () => {
      manualButton.click();
    });

    fireEvent.change(screen.getByLabelText("Card code"), { target: { value: CARD_URL } });
    await act(async () => {
      screen.getByRole("button", { name: "Look up attendee" }).click();
    });

    expect(await screen.findByText("Attendee found")).toBeInTheDocument();
    expect(screen.getByText("Marcus Lindqvist")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/scan/resolve")).toBe(true);
  });
});

describe("Scanner — rescan resets state", () => {
  it("Cancel on the confirm screen returns to the camera view", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
      }),
    );
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");

    await act(async () => {
      screen.getByRole("button", { name: "Cancel" }).click();
    });

    expect(screen.queryByText("Attendee found")).not.toBeInTheDocument();
    expect(await screen.findByText(/Point the camera/)).toBeInTheDocument();
  });

  it("'Scan another attendee' from the awarding shell also returns to the camera view", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
        "/api/staff/points/presets": () => jsonResponse(200, PRESETS),
      }),
    );
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");
    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });
    await screen.findByText("Confirmed");

    await act(async () => {
      screen.getByRole("button", { name: "Scan another attendee" }).click();
    });

    expect(screen.queryByText("Confirmed")).not.toBeInTheDocument();
    expect(await screen.findByText(/Point the camera/)).toBeInTheDocument();
  });
});

describe("Scanner — idempotency key (§13.4)", () => {
  it("is generated once when the confirm screen opens, and is NOT regenerated by clicking Confirm", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
        "/api/staff/points/presets": () => jsonResponse(200, PRESETS),
      }),
    );
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");
    expect(randomUUID).toHaveBeenCalledTimes(1);

    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });
    await screen.findByText("Confirmed");
    expect(randomUUID).toHaveBeenCalledTimes(1);

    randomUUID.mockRestore();
  });

  it("generates a fresh key for a new scan after a rescan", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      throw new Error(`unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");
    await act(async () => {
      screen.getByRole("button", { name: "Cancel" }).click();
    });
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");

    expect(randomUUID).toHaveBeenCalledTimes(2);
    randomUUID.mockRestore();
  });
});

describe("Scanner — duplicate decode does not cause repeated resolve storms", () => {
  it("only resolves once for two rapid decodes of the same frame", async () => {
    const fetchMock = fetchRouter({
      "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
      "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");

    const resolveCalls = fetchMock.mock.calls.filter(([u]) => u === "/api/staff/scan/resolve");
    expect(resolveCalls).toHaveLength(1);
  });

  it("ignores a decode that arrives while already resolving a different one", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") {
        return new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Looking up attendee…");

    // A second decode arrives before the first resolve settles — must be ignored.
    await act(async () => {
      latestOnDetected(
        "https://summit.example/c/s2oiCdKto4pseDLY4YHOtaOxEg1dRP6hGWk8qVy0-kw",
      );
    });

    await act(async () => {
      resolveFetch(jsonResponse(200, IDENTITY_B));
    });

    const resolveCalls = fetchMock.mock.calls.filter(([u]) => u === "/api/staff/scan/resolve");
    expect(resolveCalls).toHaveLength(1);
  });
});

describe("Scanner — invalid QR handling", () => {
  it("shows the generic INVALID_QR message for an unresolvable resolve, and offers rescan", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => errorResponse(404, "INVALID_QR"),
      }),
    );
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This QR code isn't valid. Ask the attendee to reopen their card.",
    );
    expect(screen.getByRole("button", { name: "Scan again" })).toBeInTheDocument();
  });

  it("rejects an arbitrary external QR without ever calling resolve", async () => {
    const fetchMock = fetchRouter({ "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME) });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());
    await act(async () => {
      latestOnDetected("https://example.com/not-facemark");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("This QR code isn't valid");
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/scan/resolve")).toBe(false);
  });
});

describe("Scanner — signed-out guard", () => {
  it("does not render the camera at all when the staff session is missing", async () => {
    vi.stubGlobal("fetch", fetchRouter({ "/api/staff/me": () => errorResponse(401, "UNAUTHENTICATED") }));
    render(<Scanner />);
    expect(await screen.findByText(/sign in as staff/)).toBeInTheDocument();
    expect(useCameraScanner).not.toHaveBeenCalled();
  });
});
