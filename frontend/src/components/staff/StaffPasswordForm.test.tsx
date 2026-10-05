import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StaffPasswordForm } from "./StaffPasswordForm";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "server text", details: {}, correlation_id: "c" } });
}

const CURRENT = "the-current-Passw0rd";
const NEW = "A-brand-new-Passw0rd-9!";

afterEach(() => vi.unstubAllGlobals());

// /api/staff/me answers `me`; the password change is answered by `change`.
function server(change: (n: number) => Response | Promise<Response>, me: () => Response = () => jsonResponse(200, { full_name: "A", role: "BOOTH_STAFF", booth_name: "B" })) {
  const changes: Record<string, string>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/staff/me") return me();
    if (url === "/api/staff/auth/password") {
      changes.push(JSON.parse(String(init?.body)));
      return change(changes.length);
    }
    return new Response(null, { status: 404 });
  }));
  return changes;
}

async function fill(current = CURRENT, next = NEW, again = NEW) {
  await screen.findByLabelText("Current password");
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("Repeat the new password"), { target: { value: again } });
}
const submit = () => fireEvent.click(screen.getByRole("button", { name: "Change password" }));

describe("StaffPasswordForm", () => {
  it("checks the session first and offers sign-in when there is none", async () => {
    server(() => new Response(null, { status: 204 }), () => errorResponse(401, "UNAUTHENTICATED"));
    render(<StaffPasswordForm />);
    expect(screen.getByText("Checking your session…")).toBeInTheDocument();
    expect(await screen.findByText("Sign in first to change your password.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Staff sign in" })).toHaveAttribute("href", "/staff/login");
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });

  it("asks for the current password and the new one twice, with the right autocomplete hints", async () => {
    server(() => new Response(null, { status: 204 }));
    render(<StaffPasswordForm />);
    await fill();
    expect(screen.getByLabelText("Current password")).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByLabelText("New password")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByLabelText("Repeat the new password")).toHaveAttribute("type", "password");
    expect(screen.getByText(/At least 12 characters/)).toBeInTheDocument();
  });

  it("refuses an empty current password, a short new one and a mismatch without asking the server", async () => {
    const changes = server(() => new Response(null, { status: 204 }));
    render(<StaffPasswordForm />);
    await fill("", NEW, NEW);
    submit();
    expect(await screen.findByText("Enter your current password.")).toBeInTheDocument();

    await fill(CURRENT, "short-1!", "short-1!");
    submit();
    expect(await screen.findByText("The new password must be at least 12 characters.")).toBeInTheDocument();

    await fill(CURRENT, NEW, NEW + "x");
    submit();
    expect(await screen.findByText("The two new passwords are not the same.")).toBeInTheDocument();
    expect(changes).toHaveLength(0);
  });

  it("success: sends exactly the three passwords, says every session ended, and keeps no password on screen", async () => {
    const changes = server(() => new Response(null, { status: 204 }));
    const { container } = render(<StaffPasswordForm />);
    await fill();
    submit();

    expect(await screen.findByText("Password changed")).toBeInTheDocument();
    expect(changes).toEqual([{ current_password: CURRENT, new_password: NEW, new_password_confirmation: NEW }]);
    expect(screen.getByRole("status")).toHaveTextContent("Every session of your account has ended, on every device");
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/staff/login");
    expect(container.querySelectorAll("input")).toHaveLength(0); // the fields (and their values) are gone
    expect(container.textContent).not.toContain(NEW);
  });

  it("claims nothing while the request is pending, and a double click is one request", async () => {
    let finish!: (r: Response) => void;
    const changes = server(() => new Promise<Response>((resolve) => { finish = resolve; }));
    render(<StaffPasswordForm />);
    await fill();
    const button = screen.getByRole("button", { name: "Change password" });
    await act(async () => { button.click(); button.click(); });
    expect(changes).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Changing password…" })).toBeDisabled();
    expect(screen.queryByText("Password changed")).not.toBeInTheDocument();
    await act(async () => { finish(new Response(null, { status: 204 })); });
    expect(await screen.findByText("Password changed")).toBeInTheDocument();
  });

  it.each([
    ["CURRENT_PASSWORD_INVALID", 400, "The current password is not correct."],
    ["PASSWORD_UNCHANGED", 400, "different from the current one"],
    ["ACCOUNT_LOCKED", 429, "Too many failed attempts"],
    ["RATE_LIMITED", 429, "Too many attempts"],
    ["VALIDATION_ERROR", 422, "need attention"],
    ["MAINTENANCE", 503, "briefly unavailable"],
  ])("%s: shows its message, keeps the new password for another try", async (code, status, message) => {
    server(() => errorResponse(status, code));
    render(<StaffPasswordForm />);
    await fill();
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText("Password changed")).not.toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toHaveValue(NEW);
    expect(screen.getByLabelText("Current password")).toHaveValue(code === "CURRENT_PASSWORD_INVALID" ? "" : CURRENT);
    expect(screen.getByRole("button", { name: "Change password" })).toBeEnabled();
  });

  it("a session that ended meanwhile says so and offers sign-in", async () => {
    server(() => errorResponse(401, "UNAUTHENTICATED"));
    render(<StaffPasswordForm />);
    await fill();
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("session has ended");
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute("href", "/staff/login");
  });

  it("after a lost answer it can be retried, and says what to do if the first try went through", async () => {
    const changes = server((n) => (n === 1 ? Promise.reject(new TypeError("offline")) : new Response(null, { status: 204 })));
    render(<StaffPasswordForm />);
    await fill();
    submit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("couldn't reach the server");
    expect(alert).toHaveTextContent("If the password was already changed, sign in with the new one.");
    expect(screen.queryByText("Password changed")).not.toBeInTheDocument();

    submit();
    expect(await screen.findByText("Password changed")).toBeInTheDocument();
    expect(changes).toHaveLength(2);
  });

  it("an unknown error code shows the generic message, never the server's text", async () => {
    server(() => errorResponse(500, "SOMETHING_NEW"));
    render(<StaffPasswordForm />);
    await fill();
    submit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(alert).not.toHaveTextContent("server text");
    await waitFor(() => expect(screen.getByRole("button", { name: "Change password" })).toBeEnabled());
  });
});
