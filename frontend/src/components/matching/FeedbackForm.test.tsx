import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FeedbackForm } from "./FeedbackForm";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

const ATTENDEE_ME = {
  first_name: "Grace",
  last_name: "Hopper",
  email: "grace@example.com",
  profession: "Systems Engineer",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("FeedbackForm — signed-out", () => {
  it("offers a sign-in link instead of a form", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(401, "UNAUTHENTICATED")));
    render(<FeedbackForm boothSlug="aiworks" />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/login",
    );
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });
});

describe("FeedbackForm — signed-in, first submission", () => {
  it("submits the selected rating and optional comment, and shows the awarded amount", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") {
        const body = JSON.parse(init?.body as string);
        expect(body).toEqual({ booth_slug: "aiworks", rating: 4, comment: "Loved it" });
        return jsonResponse(200, {
          feedback_id: 91,
          awarded_points: 5,
          already_rewarded: false,
          total_points: 50,
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);

    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "4" }).click();
    });
    fireEvent.change(screen.getByLabelText("Comment (optional)"), {
      target: { value: "Loved it" },
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });

    expect(await screen.findByText("Thanks for the feedback! +5 points awarded.")).toBeInTheDocument();
    expect(screen.getByText("New total: 50")).toBeInTheDocument();
  });

  it("does not submit an empty comment field", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") {
        const body = JSON.parse(init?.body as string);
        expect(body).toEqual({ booth_slug: "aiworks", rating: 3 });
        return jsonResponse(200, {
          feedback_id: 91,
          awarded_points: 5,
          already_rewarded: false,
          total_points: 5,
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "3" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    await screen.findByText(/points awarded/);
  });

  it("disables submit until a rating is chosen", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, ATTENDEE_ME)));
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    expect(screen.getByRole("button", { name: "Submit feedback" })).toBeDisabled();
  });

  it("shows the already-rewarded outcome distinctly, still as success", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") {
        return jsonResponse(200, {
          feedback_id: 92,
          awarded_points: 0,
          already_rewarded: true,
          total_points: 50,
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "2" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    expect(
      await screen.findByText("Thanks for the feedback! You've already earned the reward at this booth."),
    ).toBeInTheDocument();
    expect(screen.getByText("New total: 50")).toBeInTheDocument();
  });

  it("'Leave more feedback' returns to a fresh form, allowing another real submission", async () => {
    let callCount = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") {
        callCount += 1;
        return jsonResponse(200, {
          feedback_id: 90 + callCount,
          awarded_points: callCount === 1 ? 5 : 0,
          already_rewarded: callCount !== 1,
          total_points: 5,
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "5" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    await screen.findByText(/points awarded/);

    await act(async () => {
      screen.getByRole("button", { name: "Leave more feedback" }).click();
    });
    expect(await screen.findByRole("radiogroup")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit feedback" })).toBeDisabled();

    await act(async () => {
      screen.getByRole("radio", { name: "1" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    expect(await screen.findByText("Thanks for the feedback! You've already earned the reward at this booth.")).toBeInTheDocument();
    expect(callCount).toBe(2);
  });

  it("shows an error message when the server rejects the submission", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") return errorResponse(404, "BOOTH_NOT_FOUND");
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "5" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This booth couldn't be found. It may have been removed or renamed.",
    );
  });

  it("never sends a user id, booth id (beyond the slug), points, or source", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/attendees/me") return jsonResponse(200, ATTENDEE_ME);
      if (url === "/api/attendees/feedback") {
        const body = JSON.parse(init?.body as string);
        expect(Object.keys(body).sort()).toEqual(["booth_slug", "rating"]);
        return jsonResponse(200, {
          feedback_id: 1,
          awarded_points: 5,
          already_rewarded: false,
          total_points: 5,
        });
      }
      throw new Error(`unexpected fetch to ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<FeedbackForm boothSlug="aiworks" />);
    await screen.findByRole("radiogroup");
    await act(async () => {
      screen.getByRole("radio", { name: "5" }).click();
    });
    await act(async () => {
      screen.getByRole("button", { name: "Submit feedback" }).click();
    });
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => u === "/api/attendees/feedback")).toBe(true));
  });
});
