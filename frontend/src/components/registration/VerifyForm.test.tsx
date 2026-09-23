import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { VerifyForm } from "./VerifyForm";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string) {
  return jsonResponse(status, { error: { code, message: "server text", details: {}, correlation_id: "c" } });
}

let verifyResponse: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  window.sessionStorage.clear();
  window.sessionStorage.setItem("facemark.pendingEmail", "grace@summit.example.org");
  verifyResponse = () => jsonResponse(200, { status: "ACTIVE" });
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/auth/otp/verify") return verifyResponse();
    if (url === "/api/auth/otp/request") {
      return jsonResponse(200, { message: "If this email is registered, a verification code has been sent." });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function enterCode(code: string) {
  fireEvent.change(screen.getByLabelText("Verification code"), { target: { value: code } });
}

function bodyOf(url: string): unknown {
  const call = fetchMock.mock.calls.find(([u]) => u === url) as [string, RequestInit] | undefined;
  return call ? JSON.parse(call[1].body as string) : undefined;
}

describe("VerifyForm", () => {
  it("prefills the email carried over from registration", () => {
    render(<VerifyForm />);
    expect(screen.getByLabelText("Email")).toHaveValue("grace@summit.example.org");
  });

  it("accepts digits only, at most six", () => {
    render(<VerifyForm />);
    enterCode("12a3-4567");
    expect(screen.getByLabelText("Verification code")).toHaveValue("123456");
  });

  it("requires a full 6-digit code before calling the server", async () => {
    render(<VerifyForm />);
    enterCode("123");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));
    expect(await screen.findByText("Enter the 6-digit code from your email.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("verifies and shows the activated state", async () => {
    render(<VerifyForm />);
    enterCode("482913");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));

    expect(await screen.findByRole("heading", { name: "You're verified" })).toBeInTheDocument();
    expect(bodyOf("/api/auth/otp/verify")).toEqual({
      email: "grace@summit.example.org",
      code: "482913",
    });
    expect(window.sessionStorage.getItem("facemark.pendingEmail")).toBeNull();
  });

  it("shows one message for every OTP failure cause and clears the code", async () => {
    verifyResponse = () => errorResponse(400, "OTP_INVALID");
    render(<VerifyForm />);
    enterCode("000000");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));

    expect(await screen.findByText(messageFor("OTP_INVALID"))).toBeInTheDocument();
    expect(screen.getByLabelText("Verification code")).toHaveValue("");
    expect(screen.queryByRole("heading", { name: "You're verified" })).not.toBeInTheDocument();
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
  });

  it.each([
    ["MAINTENANCE", 503],
    ["INTERNAL_ERROR", 500],
  ])("handles %s without exposing internals", async (code, status) => {
    verifyResponse = () => errorResponse(status, code);
    render(<VerifyForm />);
    enterCode("482913");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));
    expect(await screen.findByText(messageFor(code))).toBeInTheDocument();
  });

  it("reports a network failure", async () => {
    verifyResponse = () => {
      throw new TypeError("Failed to fetch");
    };
    render(<VerifyForm />);
    enterCode("482913");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });

  it("requests a new code with a non-committal notice", async () => {
    render(<VerifyForm />);
    fireEvent.click(screen.getByRole("button", { name: "Send me a new code" }));

    expect(
      await screen.findByText("If this email is registered, a new code has been sent."),
    ).toBeInTheDocument();
    expect(bodyOf("/api/auth/otp/request")).toEqual({ email: "grace@summit.example.org" });
  });

  it("works without a carried-over email when one is typed in", async () => {
    window.sessionStorage.clear();
    render(<VerifyForm />);
    expect(screen.getByLabelText("Email")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "typed@summit.example.org" } });
    enterCode("482913");
    fireEvent.click(screen.getByRole("button", { name: "Verify and activate" }));
    await waitFor(() =>
      expect(bodyOf("/api/auth/otp/verify")).toEqual({
        email: "typed@summit.example.org",
        code: "482913",
      }),
    );
  });
});
