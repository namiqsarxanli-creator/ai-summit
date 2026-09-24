import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AdminPage from "./page";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("AdminPage", () => {
  it("checks the server role and shows only read-only points inspection", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/staff/auth/login") return new Response(null, { status: 204 });
      if (url === "/api/staff/me") return jsonResponse(200, { role: "ADMIN" });
      if (url === "/api/admin/points-log") return jsonResponse(200, []);
      return jsonResponse(200, []);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminPage />);
    fireEvent.change(screen.getByLabelText("Staff email"), { target: { value: "admin@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password" } });
    fireEvent.click(screen.getByText("Sign in"));
    expect(await screen.findByLabelText("Admin sections")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "points log" }));
    expect(await screen.findByText(/Read-only ledger audit/)).toBeInTheDocument();
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
});
