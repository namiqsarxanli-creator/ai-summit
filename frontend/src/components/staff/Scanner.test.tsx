import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
const TASKS = [
  {
    id: 11,
    title: "Watch the live demo",
    description: null,
    points: 7,
    repeatable: false,
    rule_revision: "rev-11-a",
  },
  {
    id: 12,
    title: "Ask about deployment",
    description: null,
    points: 13,
    repeatable: true,
    rule_revision: "rev-12-a",
  },
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
  it("shows the mandatory Attendee found / Cancel / Confirm screen before anything else, never the activity list directly", async () => {
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

    // The activity list must not be reachable without clicking Confirm.
    expect(screen.queryByLabelText("Activities")).not.toBeInTheDocument();
  });
});

describe("Scanner — activities come from the API, never hardcoded", () => {
  it("fetches the booth's activities only after Confirm, and renders exactly what the server returned", async () => {
    const fetchMock = fetchRouter({
      "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
      "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
      "/api/staff/tasks": () => jsonResponse(200, TASKS),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    await act(async () => {
      latestOnDetected(CARD_URL);
    });
    await screen.findByText("Attendee found");

    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/tasks")).toBe(false);

    await act(async () => {
      screen.getByRole("button", { name: "Confirm" }).click();
    });

    expect(await screen.findByText("Watch the live demo")).toBeInTheDocument();
    expect(screen.getByText("Ask about deployment")).toBeInTheDocument();
    expect(screen.getByText("+7")).toBeInTheDocument();
    expect(screen.getByText("+13")).toBeInTheDocument();
    // No hardcoded point values - only whatever the server returned.
    expect(screen.queryByText(/\+5\b/)).not.toBeInTheDocument();
  });

  it("says so when the booth has no awardable activity yet", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
        "/api/staff/tasks": () => jsonResponse(200, []),
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
    expect(
      await screen.findByText("No activities with a point value are set up for this booth yet."),
    ).toBeInTheDocument();
  });

  it("shows admins cannot award, without ever calling the activities endpoint", async () => {
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
    expect(fetchMock.mock.calls.some(([u]) => u === "/api/staff/tasks")).toBe(false);
  });
});

describe("Scanner — activity award flow", () => {
  const AWARD_RESULT = {
    points_log_id: 481,
    points_awarded: 7,
    resulting_total: 45,
    attendee_name: "Marcus L.",
    booth_name: "AIWorks",
    awarded_at: "2026-09-22T17:04:11Z",
    replayed: false,
    task_id: 11,
    task_title: "Watch the live demo",
  };

  type AwardHandler = (body: Record<string, unknown>) => Response | Promise<Response>;

  function rig(onAward: AwardHandler) {
    const awardBodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/tasks") return jsonResponse(200, TASKS);
      if (url === "/api/staff/points/award") {
        const body = JSON.parse(init?.body as string) as Record<string, unknown>;
        awardBodies.push(body);
        return onAward(body);
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    return { fetchMock, awardBodies };
  }

  async function reachActivityList(fetchMock: ReturnType<typeof vi.fn>): Promise<void> {
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
    await screen.findByText("Watch the live demo");
  }

  async function choose(title: string) {
    await act(async () => {
      screen.getByRole("button", { name: new RegExp(title) }).click();
    });
  }

  async function award() {
    await act(async () => {
      screen.getByRole("button", { name: /^Award \+\d+$/ }).click();
    });
  }

  it("choosing an activity opens a review with attendee, booth, activity and exact points - and sends nothing yet", async () => {
    const { fetchMock, awardBodies } = rig(() => jsonResponse(200, AWARD_RESULT));
    await reachActivityList(fetchMock);

    await choose("Watch the live demo");

    expect(await screen.findByText("Review and award")).toBeInTheDocument();
    const review = screen.getByText("Review and award").parentElement as HTMLElement;
    expect(within(review).getByText("Marcus Lindqvist")).toBeInTheDocument();
    expect(within(review).getByText("AIWorks")).toBeInTheDocument();
    expect(within(review).getByText("Watch the live demo")).toBeInTheDocument();
    expect(within(review).getByText("+7")).toBeInTheDocument();
    expect(within(review).getByText("This activity can be earned once per attendee.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Award +7" })).toBeInTheDocument();
    expect(awardBodies).toHaveLength(0);
  });

  it("Award sends the activity - never a point value - and shows the server-derived total", async () => {
    const { fetchMock, awardBodies } = rig(() => jsonResponse(200, AWARD_RESULT));
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    expect(await screen.findByText("New total: 45")).toBeInTheDocument();
    expect(awardBodies).toEqual([
      {
        card_token: "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE",
        task_id: 11,
        rule_revision: "rev-11-a",
        idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
        scan_ref: "bodyA.sigA",
      },
    ]);
    expect(awardBodies[0]).not.toHaveProperty("points");
    expect(screen.getByText(/AIWorks · Watch the live demo/)).toBeInTheDocument();
  });

  it("a double tap on Award sends exactly one request", async () => {
    let resolveAward: (r: Response) => void = () => {};
    const { fetchMock, awardBodies } = rig(
      () =>
        new Promise<Response>((resolve) => {
          resolveAward = resolve;
        }),
    );
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");

    const button = screen.getByRole("button", { name: "Award +7" });
    await act(async () => {
      button.click();
      button.click(); // a second tap before React has re-rendered
    });

    expect(awardBodies).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /^Award \+/ })).not.toBeInTheDocument();
    await act(async () => {
      resolveAward(jsonResponse(200, AWARD_RESULT));
    });
    await screen.findByText("New total: 45");
    expect(awardBodies).toHaveLength(1);
  });

  it("the idempotency key is created when an activity is reviewed, not before, and is the one sent", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    const { fetchMock, awardBodies } = rig(() => jsonResponse(200, AWARD_RESULT));
    await reachActivityList(fetchMock);
    expect(randomUUID).not.toHaveBeenCalled(); // scanning and confirming create none

    await choose("Watch the live demo");
    expect(randomUUID).toHaveBeenCalledTimes(1);
    const generated = randomUUID.mock.results[0]?.value;

    await award();
    await screen.findByText("New total: 45");
    expect(awardBodies[0]?.idempotency_key).toBe(generated);
    expect(randomUUID).toHaveBeenCalledTimes(1); // the tap did not mint another
    randomUUID.mockRestore();
  });

  it("going Back and choosing the same activity again reuses its key; another activity gets its own", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    const { fetchMock } = rig(() => jsonResponse(200, AWARD_RESULT));
    await reachActivityList(fetchMock);

    await choose("Watch the live demo");
    await act(async () => {
      screen.getByRole("button", { name: "Back" }).click();
    });
    await choose("Watch the live demo");
    expect(randomUUID).toHaveBeenCalledTimes(1);

    await act(async () => {
      screen.getByRole("button", { name: "Back" }).click();
    });
    await choose("Ask about deployment");
    expect(randomUUID).toHaveBeenCalledTimes(2);
    randomUUID.mockRestore();
  });

  it("after a lost response, Try again re-sends the SAME award with the SAME key", async () => {
    let attempt = 0;
    const { fetchMock, awardBodies } = rig(() => {
      attempt += 1;
      if (attempt === 1) throw new TypeError("network down"); // fetch itself fails
      return jsonResponse(200, { ...AWARD_RESULT, replayed: true });
    });
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    expect(await screen.findByRole("alert")).toHaveTextContent("We couldn't reach the server");
    expect(screen.getByText("Retrying is safe: the same award is never paid twice.")).toBeInTheDocument();
    await act(async () => {
      screen.getByRole("button", { name: /^Try again/ }).click();
    });

    expect(await screen.findByText("Already awarded")).toBeInTheDocument();
    expect(awardBodies).toHaveLength(2);
    expect(awardBodies[1]).toEqual(awardBodies[0]); // identical, key included
  });

  it("a refused activity (already earned) offers the list again, not a blind retry", async () => {
    const { fetchMock } = rig(() => errorResponse(409, "DUPLICATE_TASK_REWARD"));
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This attendee has already earned points for this activity.",
    );
    expect(screen.queryByRole("button", { name: /^Try again/ })).not.toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: "Choose another activity" }).click();
    });
    expect(await screen.findByText("Ask about deployment")).toBeInTheDocument();
    expect(screen.getByText("Watch the live demo")).toBeInTheDocument();
  });

  it("an activity that was switched off reloads the list so the stale one disappears", async () => {
    let listCalls = 0;
    const awardBodies: unknown[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/tasks") {
        listCalls += 1;
        return jsonResponse(200, listCalls === 1 ? TASKS : TASKS.slice(1));
      }
      if (url === "/api/staff/points/award") {
        awardBodies.push(JSON.parse(init?.body as string));
        return errorResponse(409, "TASK_INACTIVE");
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();
    expect(await screen.findByRole("alert")).toHaveTextContent("no longer active");

    await act(async () => {
      screen.getByRole("button", { name: "Choose another activity" }).click();
    });

    expect(await screen.findByText("Ask about deployment")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Watch the live demo")).not.toBeInTheDocument());
    expect(listCalls).toBe(2);
  });

  it("a rule changed after confirming pays nothing: the new rule is shown and must be confirmed again with a NEW key", async () => {
    let attempt = 0;
    const { fetchMock, awardBodies } = rig(() => {
      attempt += 1;
      if (attempt === 1) {
        return jsonResponse(409, {
          error: {
            code: "TASK_RULE_CHANGED",
            message: "",
            details: {
              task_id: 11,
              task_title: "Watch the live demo",
              points: 9,
              repeatable: false,
              rule_revision: "rev-11-b",
            },
            correlation_id: "c",
          },
        });
      }
      return jsonResponse(200, { ...AWARD_RESULT, points_awarded: 9 });
    });
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    // Back on the review screen with the NEW rule, a notice, and nothing paid yet.
    expect(await screen.findByText("Review and award")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This activity's rule changed. Nothing was awarded yet.");
    expect(screen.getByRole("alert")).toHaveTextContent("Before: +7");
    expect(screen.getByRole("alert")).toHaveTextContent("Now: +9");
    expect(screen.queryByText(/New total/)).not.toBeInTheDocument();
    expect(awardBodies).toHaveLength(1);

    // The explicit second confirmation.
    await act(async () => {
      screen.getByRole("button", { name: "Award +9" }).click();
    });
    expect(await screen.findByText("New total: 45")).toBeInTheDocument();

    expect(awardBodies.map((b) => b.rule_revision)).toEqual(["rev-11-a", "rev-11-b"]);
    expect(awardBodies[1]?.idempotency_key).not.toBe(awardBodies[0]?.idempotency_key);
    expect(awardBodies[1]).not.toHaveProperty("points");
  });

  it("a changed repeat policy alone also asks for a fresh confirmation, and says what changed", async () => {
    const { fetchMock } = rig(() =>
      jsonResponse(409, {
        error: {
          code: "TASK_RULE_CHANGED",
          message: "",
          details: {
            task_id: 11,
            task_title: "Watch the live demo",
            points: 7,
            repeatable: true,
            rule_revision: "rev-11-c",
          },
          correlation_id: "c",
        },
      }),
    );
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("Before: +7");
    expect(notice).toHaveTextContent("Now: +7");
    expect(notice).toHaveTextContent("This activity can be earned more than once.");
    expect(screen.getByRole("button", { name: "Award +7" })).toBeInTheDocument();
  });

  it("an unreadable rule-changed answer falls back to reloading the activity list", async () => {
    let listCalls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/me") return jsonResponse(200, BOOTH_STAFF_ME);
      if (url === "/api/staff/scan/resolve") return jsonResponse(200, IDENTITY_A);
      if (url === "/api/staff/tasks") {
        listCalls += 1;
        return jsonResponse(200, TASKS);
      }
      if (url === "/api/staff/points/award") return errorResponse(409, "TASK_RULE_CHANGED");
      throw new Error(`unexpected fetch to ${url}`);
    });
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    expect(await screen.findByRole("alert")).toHaveTextContent("points or repeat rule changed");
    expect(screen.queryByRole("button", { name: /^Try again/ })).not.toBeInTheDocument();
    await act(async () => {
      screen.getByRole("button", { name: "Choose another activity" }).click();
    });
    expect(await screen.findByText("Ask about deployment")).toBeInTheDocument();
    expect(listCalls).toBe(2);
  });

  it("a network retry keeps the confirmed revision and key, so a rule change in between cannot cause a second payment", async () => {
    let attempt = 0;
    const { fetchMock, awardBodies } = rig(() => {
      attempt += 1;
      if (attempt === 1) throw new TypeError("network down");
      return jsonResponse(200, { ...AWARD_RESULT, replayed: true });
    });
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();
    await screen.findByRole("alert");
    await act(async () => {
      screen.getByRole("button", { name: /^Try again/ }).click();
    });
    await screen.findByText("Already awarded");

    expect(awardBodies).toHaveLength(2);
    expect(awardBodies[1]?.rule_revision).toBe("rev-11-a");
    expect(awardBodies[1]?.idempotency_key).toBe(awardBodies[0]?.idempotency_key);
  });

  it("a page older than the server contract is told to reload, with no retry that could never work", async () => {
    const reload = vi.fn();
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...original, reload },
    });
    try {
      const { fetchMock } = rig(() =>
        jsonResponse(403, {
          error: {
            code: "CSRF_REJECTED",
            message: "This page is out of date.",
            details: { reason: "client_outdated" },
            correlation_id: "c",
          },
        }),
      );
      await reachActivityList(fetchMock);
      await choose("Watch the live demo");
      await award();

      expect(await screen.findByRole("alert")).toHaveTextContent("Please reload the page and try again.");
      expect(screen.getByText("No points were awarded.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Try again/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Choose another activity" })).not.toBeInTheDocument();

      await act(async () => {
        screen.getByRole("button", { name: "Reload the page" }).click();
      });
      expect(reload).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: original });
    }
  });

  it("an expired scan can only be answered with a fresh scan", async () => {
    const { fetchMock } = rig(() => errorResponse(400, "SCAN_EXPIRED"));
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();

    expect(await screen.findByRole("alert")).toHaveTextContent("That scan expired. Scan again.");
    expect(screen.queryByRole("button", { name: /^Try again/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Choose another activity" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan another attendee" })).toBeInTheDocument();
  });

  it("replayed awards are labelled distinctly from a fresh award", async () => {
    const { fetchMock } = rig(() => jsonResponse(200, { ...AWARD_RESULT, replayed: true }));
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();
    expect(await screen.findByText("Already awarded")).toBeInTheDocument();
  });

  it("after a success another activity can be awarded to the same attendee with its own key", async () => {
    const { fetchMock, awardBodies } = rig((body) =>
      jsonResponse(200, {
        ...AWARD_RESULT,
        points_awarded: body.task_id === 12 ? 13 : 7,
        task_id: body.task_id,
        task_title: body.task_id === 12 ? "Ask about deployment" : "Watch the live demo",
      }),
    );
    await reachActivityList(fetchMock);
    await choose("Watch the live demo");
    await award();
    await screen.findByText("New total: 45");

    await act(async () => {
      screen.getByRole("button", { name: "Award another activity" }).click();
    });
    await choose("Ask about deployment");
    expect(screen.getByText("This activity can be earned more than once.")).toBeInTheDocument();
    await award();
    await screen.findByText(/Ask about deployment/);

    expect(awardBodies.map((b) => b.task_id)).toEqual([11, 12]);
    expect(awardBodies[0]?.idempotency_key).not.toBe(awardBodies[1]?.idempotency_key);
    expect(awardBodies[0]?.scan_ref).toBe(awardBodies[1]?.scan_ref); // one scan, two activities
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
        "/api/staff/tasks": () => jsonResponse(200, TASKS),
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

describe("Scanner — idempotency keys (§13.4)", () => {
  it("creates none while scanning or confirming, only when an activity is reviewed", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
        "/api/staff/tasks": () => jsonResponse(200, TASKS),
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
    expect(randomUUID).not.toHaveBeenCalled();

    await act(async () => {
      screen.getByRole("button", { name: /Watch the live demo/ }).click();
    });
    expect(randomUUID).toHaveBeenCalledTimes(1);
    randomUUID.mockRestore();
  });

  it("a new scan never inherits the previous scan's key for the same activity", async () => {
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        "/api/staff/me": () => jsonResponse(200, BOOTH_STAFF_ME),
        "/api/staff/scan/resolve": () => jsonResponse(200, IDENTITY_A),
        "/api/staff/tasks": () => jsonResponse(200, TASKS),
      }),
    );
    render(<Scanner />);
    await waitFor(() => expect(useCameraScanner).toHaveBeenCalled());

    for (let scan = 0; scan < 2; scan += 1) {
      await act(async () => {
        latestOnDetected(CARD_URL);
      });
      await screen.findByText("Attendee found");
      await act(async () => {
        screen.getByRole("button", { name: "Confirm" }).click();
      });
      await act(async () => {
        screen.getByRole("button", { name: /Watch the live demo/ }).click();
      });
      await screen.findByText("Review and award");
      await act(async () => {
        screen.getByRole("button", { name: "Back" }).click();
      });
      await act(async () => {
        screen.getByRole("button", { name: "Scan another attendee" }).click();
      });
    }

    expect(randomUUID).toHaveBeenCalledTimes(2); // one per scan, not one in total
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
