import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StaffResetForm } from "./StaffResetForm";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "server text", details: {}, correlation_id: "c" } });
}

const NEW = "A-brand-new-Passw0rd-9!";
const GENERIC = "If this address belongs to an active staff account, a code has been sent to it. It is valid for 10 minutes.";

afterEach(() => vi.unstubAllGlobals());

type Calls = { request: Record<string, string>[]; confirm: Record<string, string>[] };

function server(
  request: (n: number) => Response | Promise<Response> = () => jsonResponse(200, { message: "x" }),
  confirm: (n: number) => Response | Promise<Response> = () => new Response(null, { status: 204 }),
): Calls {
  const calls: Calls = { request: [], confirm: [] };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (url === "/api/staff/auth/password-reset/request") { calls.request.push(body); return request(calls.request.length); }
    if (url === "/api/staff/auth/password-reset/confirm") { calls.confirm.push(body); return confirm(calls.confirm.length); }
    return new Response(null, { status: 404 });
  }));
  return calls;
}

function askFor(email = "staff@example.com") {
  fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: email } });
  fireEvent.click(screen.getByRole("button", { name: "Send the code" }));
}

async function fillSecond(code = "123456", next = NEW, again = NEW) {
  await screen.findByLabelText("Code from the email");
  fireEvent.change(screen.getByLabelText("Code from the email"), { target: { value: code } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("Repeat the new password"), { target: { value: again } });
}
const setPassword = () => fireEvent.click(screen.getByRole("button", { name: "Set the new password" }));

describe("StaffResetForm — asking for the code", () => {
  it("validates the address first", () => {
    const calls = server();
    render(<StaffResetForm />);
    askFor("not-an-address");
    expect(screen.getByText("Enter your staff email address.")).toBeInTheDocument();
    expect(calls.request).toHaveLength(0);
  });

  it("sends the address and the page language, then shows the same message whatever the address is", async () => {
    const calls = server();
    render(<StaffResetForm />);
    askFor("  nobody@example.com ");
    expect(await screen.findByText(GENERIC)).toBeInTheDocument();
    expect(calls.request).toEqual([{ email: "nobody@example.com", language: "en" }]);
    expect(screen.getByLabelText("Code from the email")).toBeInTheDocument();
    // nothing on the screen says whether the address exists
    expect(screen.queryByText(/not found|no such|unknown|does not exist/i)).not.toBeInTheDocument();
  });

  it("a double click on Send is one request", async () => {
    let finish!: (r: Response) => void;
    const calls = server(() => new Promise<Response>((resolve) => { finish = resolve; }));
    render(<StaffResetForm />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "staff@example.com" } });
    const button = screen.getByRole("button", { name: "Send the code" });
    await act(async () => { button.click(); button.click(); });
    expect(calls.request).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
    await act(async () => { finish(jsonResponse(200, { message: "x" })); });
    expect(await screen.findByText(GENERIC)).toBeInTheDocument();
  });

  it("when the request is rate limited the page says so, and 'I already have a code' still works", async () => {
    server(() => errorResponse(429, "RATE_LIMITED"));
    render(<StaffResetForm />);
    askFor();
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts");
    expect(screen.queryByLabelText("Code from the email")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "I already have a code" }));
    expect(await screen.findByLabelText("Code from the email")).toBeInTheDocument();
    expect(screen.queryByText(GENERIC)).not.toBeInTheDocument(); // no claim that a code was sent
  });

  it("'I already have a code' needs a valid address first", () => {
    render(<StaffResetForm />);
    fireEvent.click(screen.getByRole("button", { name: "I already have a code" }));
    expect(screen.getByText("Enter your staff email address.")).toBeInTheDocument();
  });
});

describe("StaffResetForm — setting the new password", () => {
  async function toSecondStep(calls = server()) {
    render(<StaffResetForm />);
    askFor();
    await screen.findByLabelText("Code from the email");
    return calls;
  }

  it("validates the code and the password locally", async () => {
    const calls = await toSecondStep();
    await fillSecond("12ab", NEW, NEW);
    setPassword();
    expect(await screen.findByText("Enter the 6-digit code from your email.")).toBeInTheDocument();
    await fillSecond("123456", "short-1!", "short-1!");
    setPassword();
    expect(await screen.findByText("The new password must be at least 12 characters.")).toBeInTheDocument();
    await fillSecond("123456", NEW, NEW + "!");
    setPassword();
    expect(await screen.findByText("The two new passwords are not the same.")).toBeInTheDocument();
    expect(calls.confirm).toHaveLength(0);
  });

  it("success: sends the address, code and password twice, ends on sign-in and keeps no secret on screen", async () => {
    const calls = await toSecondStep();
    await fillSecond();
    setPassword();

    expect(await screen.findByText("Password reset")).toBeInTheDocument();
    expect(calls.confirm).toEqual([
      { email: "staff@example.com", code: "123456", new_password: NEW, new_password_confirmation: NEW },
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("Sign in with the new password");
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute("href", "/staff/login");
    expect(document.body.textContent).not.toContain(NEW);
    expect(document.querySelectorAll("input")).toHaveLength(0);
  });

  it("a wrong or used code gets the one OTP message and the code field is cleared", async () => {
    server(undefined, () => errorResponse(400, "OTP_INVALID"));
    render(<StaffResetForm />);
    askFor();
    await fillSecond();
    setPassword();
    expect(await screen.findByRole("alert")).toHaveTextContent("That code didn't work");
    expect(screen.getByLabelText("Code from the email")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue(NEW); // the password is kept for the next try
    expect(screen.queryByText("Password reset")).not.toBeInTheDocument();
  });

  it("claims nothing while pending, and a double click is one request", async () => {
    let finish!: (r: Response) => void;
    const calls = server(undefined, () => new Promise<Response>((resolve) => { finish = resolve; }));
    render(<StaffResetForm />);
    askFor();
    await fillSecond();
    const button = screen.getByRole("button", { name: "Set the new password" });
    await act(async () => { button.click(); button.click(); });
    expect(calls.confirm).toHaveLength(1);
    expect(screen.queryByText("Password reset")).not.toBeInTheDocument();
    await act(async () => { finish(new Response(null, { status: 204 })); });
    expect(await screen.findByText("Password reset")).toBeInTheDocument();
  });

  it.each([
    ["RATE_LIMITED", 429, "Too many attempts"],
    ["MAINTENANCE", 503, "briefly unavailable"],
    ["VALIDATION_ERROR", 422, "need attention"],
  ])("%s: shows its message and allows another try", async (code, status, message) => {
    server(undefined, () => errorResponse(status, code));
    render(<StaffResetForm />);
    askFor();
    await fillSecond();
    setPassword();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "Set the new password" })).toBeEnabled();
  });

  it("after a lost answer it says what to do if the first try went through, and can be retried", async () => {
    const calls = server(undefined, (n) => (n === 1 ? Promise.reject(new TypeError("offline")) : new Response(null, { status: 204 })));
    render(<StaffResetForm />);
    askFor();
    await fillSecond();
    setPassword();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("couldn't reach the server");
    expect(alert).toHaveTextContent("If the password was already changed, sign in with the new one.");
    expect(screen.getByLabelText("Code from the email")).toHaveValue("123456"); // kept: the retry needs it
    setPassword();
    expect(await screen.findByText("Password reset")).toBeInTheDocument();
    expect(calls.confirm).toHaveLength(2);
  });

  it("'Send a new code' goes back to the address step and sends a fresh request", async () => {
    const calls = server();
    render(<StaffResetForm />);
    askFor();
    await screen.findByLabelText("Code from the email");
    fireEvent.click(screen.getByRole("button", { name: "Send a new code" }));
    expect(screen.getByLabelText("Staff email")).toHaveValue("staff@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Send the code" }));
    await screen.findByLabelText("Code from the email");
    expect(calls.request).toHaveLength(2);
  });
});
