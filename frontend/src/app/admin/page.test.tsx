import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/components/ui/LanguageProvider";

import AdminPage from "./page";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminPage", () => {
  it("names the event, not the project, and labels the sign-in form in the chosen language", async () => {
    window.localStorage.setItem("facemark.language", "az");
    try {
      vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { error: { code: "UNAUTHENTICATED" } })));
      render(<LanguageProvider><AdminPage /></LanguageProvider>);
      expect(await screen.findByRole("heading", { level: 1, name: "AI Summit idarəetmə paneli" })).toBeInTheDocument();
      expect(screen.getByLabelText("Şifrə")).toHaveAttribute("type", "password");
      expect(screen.queryByText(/FACEMARK/)).not.toBeInTheDocument();
    } finally {
      window.localStorage.removeItem("facemark.language");
    }
  });

  it("checks the server role and shows only read-only points inspection", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url.startsWith("/api/admin/points-log")) return jsonResponse(200, { items: [], next_cursor: null, limit: 50 });
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByText("Sign in"));
    expect(await screen.findByLabelText("Admin sections")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Points ledger" }));
    expect(await screen.findByText(/The ledger itself is never edited/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /award points/i })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/admin/points-log", expect.anything());
  });

  it("does not show admin controls to a booth-staff session", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url === "/api/staff/me" ? jsonResponse(200, { role: "BOOTH_STAFF" }) : new Response(null, { status: 204 }),
    ));
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "staff@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByText("Sign in"));
    expect(await screen.findByRole("alert")).toHaveTextContent("not an administrator");
    expect(screen.queryByLabelText("Admin sections")).not.toBeInTheDocument();
  });

  it("confirms deactivation and reports a saved mutation without offering points editing", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/staff/7/deactivate") return jsonResponse(200, { id: 7, is_active: false });
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Staff$/ }));
    fireEvent.change(screen.getByLabelText("Staff ID to deactivate"), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Deactivate staff" }));
    expect(confirm).toHaveBeenCalled();
    expect(await screen.findByText(/Change saved and recorded/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/admin/staff/7/deactivate", expect.anything());
    expect(screen.queryByRole("button", { name: /award points/i })).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("shows email delivery status without any code or full address and pages older rows", async () => {
    const row = (id: number) => ({
      id,
      email_masked: "g***@summit.example.org",
      status: id === 3 ? "failed" : "accepted",
      reason: id === 3 ? "The email provider rejected the API key or the sending domain." : null,
    });
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/otp-deliveries")
        return jsonResponse(200, {
          items: [row(3), row(2)],
          next_before_id: 2,
          summary: { counts: { pending: 0, processing: 0, accepted: 1, failed: 1, expired: 0 }, due_now: 0 },
          note: "accepted means the email provider took the message, not that it reached an inbox.",
        });
      if (url === "/api/admin/otp-deliveries?before_id=2")
        return jsonResponse(200, {
          items: [row(1)],
          next_before_id: null,
          summary: { counts: {}, due_now: 0 },
          note: "",
        });
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    fireEvent.click(await screen.findByRole("button", { name: "Email delivery" }));

    expect(await screen.findByText(/not that it reached an inbox/)).toBeInTheDocument();
    expect(screen.getByText(/rejected the API key/)).toBeInTheDocument();
    expect(screen.getAllByText("g***@summit.example.org")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /award points|create|deactivate/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    await waitFor(() => expect(screen.getAllByText("g***@summit.example.org")).toHaveLength(3));
    expect(screen.queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/admin/otp-deliveries?before_id=2", expect.anything());
  });

  it("creates an activity with its point rule and repeat policy, and edits them separately", async () => {
    const bodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/tasks" && init?.method === "POST") {
        bodies.push(JSON.parse(init.body as string));
        return jsonResponse(201, { id: 31, points: 12 });
      }
      if (url.startsWith("/api/admin/tasks/") && init?.method === "PATCH") {
        bodies.push({ url, ...JSON.parse(init.body as string) });
        return jsonResponse(200, { id: 31 });
      }
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: "Tasks" }));

    fireEvent.change(screen.getByLabelText("Booth ID"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Quiz corner" } });
    fireEvent.change(screen.getByLabelText("Points per award (1–100)"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Repeat policy"), { target: { value: "allow" } });
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ booth_id: 4, title: "Quiz corner", points: 12, allow_repeat_points: true });

    fireEvent.change(screen.getByLabelText("Task ID"), { target: { value: "31" } });
    fireEvent.change(screen.getByLabelText("Repeat policy"), { target: { value: "inherit" } });
    fireEvent.click(screen.getByRole("button", { name: "Set repeat policy" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({ url: "/api/admin/tasks/31", allow_repeat_points: null });

    fireEvent.change(screen.getByLabelText("Points per award (1–100)"), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Set task points" }));
    await waitFor(() => expect(bodies).toHaveLength(3));
    expect(bodies[2]).toEqual({ url: "/api/admin/tasks/31", points: 20 });
  });

  function deferred() {
    let resolve!: (value: Response) => void;
    const promise = new Promise<Response>((r) => { resolve = r; });
    return { promise, resolve };
  }

  async function signIn(): Promise<void> {
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByLabelText("Admin sections");
  }

  it("a slow answer for a section the admin already left never appears under the new section", async () => {
    const slowBooths = deferred();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/booths") return slowBooths.promise;
      if (url === "/api/admin/interests") return jsonResponse(200, [{ id: 1, name: "Robotics interest" }]);
      return jsonResponse(200, { items: [], next_cursor: null, limit: 50 });
    }));
    render(<AdminPage />);
    await signIn();

    fireEvent.click(screen.getByRole("button", { name: "Booths" }));      // slow
    fireEvent.click(screen.getByRole("button", { name: "Interests" }));   // fast
    expect(await screen.findByText("Robotics interest")).toBeInTheDocument();

    await act(async () => { slowBooths.resolve(jsonResponse(200, [{ id: 7, name: "Late booth" }])); });
    expect(screen.getByText("Robotics interest")).toBeInTheDocument();
    expect(screen.queryByText("Late booth")).not.toBeInTheDocument();
  });

  it("the refresh that follows a saved change cannot paint its old section over the one now open", async () => {
    const slowBoothsRefresh = deferred();
    let boothReads = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/booths" && (init?.method ?? "GET") === "GET") {
        boothReads += 1;
        return boothReads === 1 ? jsonResponse(200, [{ id: 3, name: "First booth" }]) : slowBoothsRefresh.promise;
      }
      if (url.startsWith("/api/admin/booths/") && init?.method === "PATCH") return jsonResponse(200, { id: 3 });
      if (url === "/api/admin/interests") return jsonResponse(200, [{ id: 1, name: "Robotics interest" }]);
      return jsonResponse(200, { items: [], next_cursor: null, limit: 50 });
    }));
    render(<AdminPage />);
    await signIn();
    fireEvent.click(screen.getByRole("button", { name: "Booths" }));
    await screen.findByText("First booth");

    fireEvent.change(screen.getByLabelText("Booth ID"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Rename booth" }));
    await waitFor(() => expect(boothReads).toBe(2)); // the refresh is in flight and slow

    fireEvent.click(screen.getByRole("button", { name: "Interests" }));
    expect(await screen.findByText("Robotics interest")).toBeInTheDocument();
    await act(async () => { slowBoothsRefresh.resolve(jsonResponse(200, [{ id: 3, name: "Renamed booth" }])); });

    expect(screen.getByText("Robotics interest")).toBeInTheDocument();
    expect(screen.queryByText("Renamed booth")).not.toBeInTheDocument();
  });

  it("a change saved while the attendee list is open reloads that list from its first page", async () => {
    const reads: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url.startsWith("/api/admin/attendees")) {
        reads.push(url);
        return jsonResponse(200, { items: [{ id: 1, first_name: "Ada", last_name: "L", email: "a@x.test" }], next_cursor: null, limit: 50 });
      }
      if (init?.method === "POST") return jsonResponse(200, { id: 9 });
      return jsonResponse(200, []);
    }));
    render(<AdminPage />);
    await signIn();
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[1]).toBe("/api/admin/attendees");
  });

  it("labels the old booth presets as legacy and not affecting activity points", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url === "/api/staff/me" ? jsonResponse(200, { role: "ADMIN" }) : jsonResponse(200, []),
    ));
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: "Point presets (legacy)" }));

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Legacy — these presets no longer affect new activity points.");
    expect(note).toHaveTextContent("not used when awarding");
    // The data stays manageable (nothing is removed) - only labelled.
    expect(screen.getByRole("button", { name: "Create preset" })).toBeInTheDocument();
  });

  it("lets an administrator start a reset email for a staff member - confirmed first, and without ever seeing a code or choosing a password", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/staff/7/password-reset") return jsonResponse(202, { status: "queued" });
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Staff$/ }));

    // a bad id is refused on the page
    fireEvent.click(screen.getByRole("button", { name: "Send password reset email" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a whole number greater than zero.");
    expect(confirm).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Staff ID for the reset email"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Email language"), { target: { value: "ru" } });
    fireEvent.click(screen.getByRole("button", { name: "Send password reset email" }));
    expect(confirm).toHaveBeenCalledTimes(1); // declined: nothing was sent
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("password-reset"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Send password reset email" }));
    expect(await screen.findByText(/A reset email was queued for that staff member/)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([u]) => u === "/api/admin/staff/7/password-reset") as unknown as [string, RequestInit];
    expect(JSON.parse(String(call[1].body))).toEqual({ language: "ru" }); // the language and nothing else
    expect(screen.getByText(/You cannot see the code/)).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("explains a reset email that could not be started", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/staff/7/password-reset") return jsonResponse(503, { error: { code: "PASSWORD_RESET_UNAVAILABLE", message: "x", details: {}, correlation_id: "c" } });
      return jsonResponse(200, []);
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Staff$/ }));
    fireEvent.change(screen.getByLabelText("Staff ID for the reset email"), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Send password reset email" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Email is not set up on this server");
    expect(screen.queryByText(/A reset email was queued/)).not.toBeInTheDocument();
  });

  it("links an administrator to their own password change", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      return jsonResponse(200, { items: [], next_cursor: null, limit: 50 });
    }));
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("link", { name: "Change my password" })).toHaveAttribute("href", "/staff/password");
  });

  it("has a read-only Audit log section: its own list, no management form, nothing written by looking", async () => {
    const event = {
      id: 1, created_at: "2026-09-01T10:00:00Z", actor_id: 1, actor_name: "Ada Admin", action: "booth.create",
      target_type: "booth", target_id: "4", operation_id: null, before: null, after: { name: "Hall" },
      changed_fields: ["name"], result: null, hidden_field_count: 0, details: "known",
    };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      void init;
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url.startsWith("/api/admin/audit")) return jsonResponse(200, { items: [event], next_cursor: null, limit: 50 });
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    fireEvent.click(await screen.findByRole("button", { name: "Audit log" }));

    expect(await screen.findByRole("list", { name: "Audit events" })).toHaveTextContent("Booth created");
    expect(screen.getByRole("heading", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Manage/ })).not.toBeInTheDocument(); // no edit form
    const auditCalls = () => fetchMock.mock.calls.filter(([url]) => String(url).startsWith("/api/admin/audit"));
    expect(auditCalls()).toHaveLength(1);

    // Refresh reloads the audit list; viewing it only ever reads
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(auditCalls()).toHaveLength(2));
    const writes = fetchMock.mock.calls.filter(([, init]) => {
      const method = (init as RequestInit | undefined)?.method;
      return method !== undefined && method !== "GET";
    });
    expect(writes.map(([url]) => url)).toEqual(["/api/staff/auth/login"]);
  });
});
