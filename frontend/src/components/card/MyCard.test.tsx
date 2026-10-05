import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { messageFor } from "@/lib/errors";
import { decodeQrSvg } from "@/test-utils/qr";

import { MyCard } from "./MyCard";

const CARD_URL = "http://localhost:3000/c/GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";
const CARD = {
  first_name: "Marcus",
  last_name: "Lindqvist",
  profession: "Data Platform Engineer",
  interests: [{ slug: "data-analytics", name: "Data Analytics" }],
  contact: { type: "EMAIL", value: "marcus.lindqvist@attendee.facemark.demo" },
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse(status, { error: { code, message: "", details: {}, correlation_id: "c" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.FacemarkOfflineQr;
  window.localStorage.removeItem("facemark.offlineQr.v1");
});

describe("MyCard", () => {
  it("shows a QR that decodes to exactly the server's card URL, plus the card preview", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { card_url: CARD_URL, card: CARD }));
    vi.stubGlobal("fetch", fetchMock);
    render(<MyCard />);

    const svg = await screen.findByRole("img", { name: /QR code for Marcus Lindqvist/ });
    const payload = decodeQrSvg(svg);
    expect(payload).toBe(CARD_URL);
    // The payload carries none of the attendee's details.
    for (const detail of ["Marcus", "Lindqvist", "marcus.lindqvist", "Data", "@"]) {
      expect(payload).not.toContain(detail);
    }

    expect(screen.getByRole("heading", { level: 2, name: "Marcus Lindqvist" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Email Marcus" })).toBeInTheDocument();
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url]) => url)).toEqual(["/api/attendees/me/card"]);
  });

  it("saves an offline QR drawing only after the attendee chooses and confirms a PIN", async () => {
    const save = vi.fn(async () => {});
    window.FacemarkOfflineQr = { save, hasCopy: () => false, clear: () => {} };
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { card_url: CARD_URL, card: CARD })));
    render(<MyCard />);
    await screen.findByRole("img", { name: /QR code for Marcus Lindqvist/ });
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Choose a 6–12 digit PIN"), { target: { value: "123456" } });
    fireEvent.change(screen.getByLabelText("Repeat PIN"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Save offline QR" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const [pin, drawing] = save.mock.calls[0] as unknown as [string, { dim: number; path: string }];
    expect(pin).toBe("123456");
    expect(drawing.dim).toBeGreaterThan(29);
    expect(drawing.path).toMatch(/^M/);
    expect(await screen.findByText("Offline QR saved on this device.")).toBeInTheDocument();
  });
  it("refuses to encode anything that is not a bare card URL", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, { card_url: `${CARD_URL}?email=marcus@example.com`, card: CARD }),
      ),
    );
    render(<MyCard />);
    expect(await screen.findByText(messageFor("INTERNAL_ERROR"))).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it.each([
    ["UNAUTHENTICATED", 401],
    ["REGISTRATION_NOT_VERIFIED", 403],
  ])("asks a signed-out visitor (%s) to sign in", async (code, status) => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(status, code)));
    render(<MyCard />);
    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("reports other failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => errorResponse(503, "MAINTENANCE")));
    render(<MyCard />);
    expect(await screen.findByText(messageFor("MAINTENANCE"))).toBeInTheDocument();
  });
});
