import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";

import { StaffLoginForm, returnPath } from "./StaffLoginForm";

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

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  push.mockReset();
  fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function submit(email: string, password: string) {
  render(<StaffLoginForm />);
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("StaffLoginForm", () => {
  it("returns to the staff page the sign-in was opened from", async () => {
    window.history.pushState({}, "", "/staff/login?next=/staff/final");
    try {
      await submit("organiser@example.com", "hunter2");
      await waitFor(() => expect(push).toHaveBeenCalledWith("/staff/final"));
    } finally {
      window.history.pushState({}, "", "/");
    }
  });

  it("leads the FACEMARK sign-in to the FACEMARK section", async () => {
    render(<StaffLoginForm home="/facemark" />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "organiser@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/facemark"));
  });

  it("never returns to another site or an unknown path: the staff page instead", async () => {
    for (const next of ["https://evil.example/", "//evil.example", "/admin", "/facemark/../admin", "javascript:alert(1)"]) {
      expect(returnPath(`?next=${encodeURIComponent(next)}`)).toBe("/staff");
    }
    expect(returnPath("")).toBe("/staff");
    expect(returnPath("?next=/staff/final")).toBe("/staff/final");
    expect(returnPath("?next=/facemark", "/staff")).toBe("/facemark");
    expect(returnPath("?next=https://evil.example/", "/facemark")).toBe("/facemark");
  });
  it("links to the password reset page", () => {
    render(<StaffLoginForm />);
    expect(screen.getByRole("link", { name: "Forgot your password?" })).toHaveAttribute("href", "/staff/reset");
  });

  it("validates the email before submitting", async () => {
    render(<StaffLoginForm />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "nope" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter your staff email address.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requires a password before submitting", async () => {
    render(<StaffLoginForm />);
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "staff@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter your password.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("submits email and password and routes to the staff panel on success", async () => {
    await submit("staff@example.com", "hunter2");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/staff"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/staff/auth/login");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "staff@example.com",
      password: "hunter2",
    });
  });

  it("shows one message for invalid credentials and clears the password", async () => {
    fetchMock.mockImplementation(async () => errorResponse(401, "INVALID_CREDENTIALS"));
    await submit("staff@example.com", "wrong");
    expect(await screen.findByText(messageFor("INVALID_CREDENTIALS"))).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(push).not.toHaveBeenCalled();
    expect(screen.queryByText("server text")).not.toBeInTheDocument();
  });

  it("explains an account lockout", async () => {
    fetchMock.mockImplementation(async () => errorResponse(429, "ACCOUNT_LOCKED"));
    await submit("staff@example.com", "whatever");
    expect(await screen.findByText(messageFor("ACCOUNT_LOCKED"))).toBeInTheDocument();
  });

  it("never sends a booth id, staff id or role in the request body", async () => {
    await submit("staff@example.com", "hunter2");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(Object.keys(body).sort()).toEqual(["email", "password"]);
  });

  it("reports a network failure", async () => {
    fetchMock.mockImplementation(async () => {
      throw new TypeError("Failed to fetch");
    });
    await submit("staff@example.com", "hunter2");
    expect(await screen.findByText(messageFor("NETWORK_ERROR"))).toBeInTheDocument();
  });

  it("has no link to a staff registration page", () => {
    render(<StaffLoginForm />);
    // the only link on the form is the way back into an EXISTING account
    expect(screen.getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/staff/reset"]);
  });
});
