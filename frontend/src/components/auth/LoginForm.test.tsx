import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { LoginForm } from "./LoginForm";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, {
    error: { code, message: "server text", details: {}, correlation_id: "c" },
  });
}

const EMAIL = "marcus.lindqvist@attendee.facemark.demo";

let requestResponse: () => Response;
let verifyResponse: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  push.mockReset();
  requestResponse = () =>
    jsonResponse(200, { message: "If this email is registered, a verification code has been sent." });
  verifyResponse = () => jsonResponse(200, { status: "ACTIVE" });
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/auth/otp/request") return requestResponse();
    if (url === "/api/auth/otp/verify") return verifyResponse();
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function bodies(url: string): unknown[] {
  return fetchMock.mock.calls
    .filter(([u]) => u === url)
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

async function reachCodeStep() {
  render(<LoginForm />);
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: EMAIL } });
  fireEvent.click(screen.getByRole("button", { name: "Send me a sign-in code" }));
  await screen.findByLabelText("Sign-in code");
}

describe("LoginForm", () => {
  it("validates the email before asking for a code", async () => {
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Send me a sign-in code" }));
    expect(await screen.findByText("Enter the email you registered with.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requests a code with non-committal copy, then asks for it", async () => {
    await reachCodeStep();
    expect(bodies("/api/auth/otp/request")).toEqual([{ email: EMAIL, language: "en" }]);
    expect(
      screen.getByText("If this email is registered, we've sent a 6-digit sign-in code."),
    ).toBeInTheDocument();
  });

  it("verifies the code and returns home signed in", async () => {
    await reachCodeStep();
    fireEvent.change(screen.getByLabelText("Sign-in code"), { target: { value: "482913" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"));
    expect(bodies("/api/auth/otp/verify")).toEqual([{ email: EMAIL, code: "482913" }]);
  });

  it("requires six digits before verifying", async () => {
    await reachCodeStep();
    fireEvent.change(screen.getByLabelText("Sign-in code"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter the 6-digit code from your email.")).toBeInTheDocument();
    expect(bodies("/api/auth/otp/verify")).toEqual([]);
  });

  it("shows one message for any OTP failure and clears the code", async () => {
    verifyResponse = () => errorResponse(400, "OTP_INVALID");
    await reachCodeStep();
    fireEvent.change(screen.getByLabelText("Sign-in code"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(messageFor("OTP_INVALID"))).toBeInTheDocument();
    expect(screen.getByLabelText("Sign-in code")).toHaveValue("");
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
  });

  it("explains a rate limit and stays on the email step", async () => {
    requestResponse = () => errorResponse(429, "RATE_LIMITED");
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: EMAIL } });
    fireEvent.click(screen.getByRole("button", { name: "Send me a sign-in code" }));
    expect(await screen.findByText(messageFor("RATE_LIMITED"))).toBeInTheDocument();
    expect(screen.queryByLabelText("Sign-in code")).not.toBeInTheDocument();
  });

  it("can send a new code and switch back to a different email", async () => {
    await reachCodeStep();
    fireEvent.click(screen.getByRole("button", { name: "Send a new code" }));
    await waitFor(() => expect(bodies("/api/auth/otp/request")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Use a different email" }));
    expect(screen.getByLabelText("Email")).toHaveValue(EMAIL);
  });

  it("persists a device only with explicit opt-in", async () => {
    await reachCodeStep();
    const choice = screen.getByRole("checkbox", { name: "Remember me on this device for 30 days" });
    expect(choice).not.toBeChecked();fireEvent.click(choice);
    fireEvent.change(screen.getByLabelText("Sign-in code"), { target: { value: "482913" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/"));
    expect(bodies("/api/auth/otp/verify")).toEqual([{ email: EMAIL, code: "482913", remember_device: true }]);
  });

  it("reports a network failure", async () => {
    verifyResponse = () => {
      throw new TypeError("Failed to fetch");
    };
    await reachCodeStep();
    fireEvent.change(screen.getByLabelText("Sign-in code"), { target: { value: "482913" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });
});
