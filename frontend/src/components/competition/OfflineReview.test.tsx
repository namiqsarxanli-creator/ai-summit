import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OfflineReview, verifiedIso } from "./OfflineReview";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const ROW = {
  id: 7, kind: "mentor_complete", target_id: 4, phase: null, attendee: "Leyla M.", scanned_by: "Host",
  device_scanned_at: "2026-11-13T11:52:00Z", received_at: "2026-11-13T13:45:00Z", card_evidence: true, own_scan: false,
};

afterEach(() => vi.unstubAllGlobals());

describe("verifiedIso", () => {
  it("puts the reviewer's HH:MM on the scan's event day with the event's offset", () => {
    expect(verifiedIso("2026-11-13T11:52:00Z", "15:52", "Asia/Baku")).toBe("2026-11-13T15:52:00+04:00");
    expect(verifiedIso("2026-11-13T11:52:00Z", "3pm", "Asia/Baku")).toBeNull();
  });
});

describe("OfflineReview", () => {
  function server(rows: unknown[]) {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return json(200, { status: "failed", outcome: { reason: "card_revoked" } });
      }
      return json(200, { scans: rows });
    }));
    return posts;
  }

  it("sends the verified time, never just the phone's, and explains a refusal", async () => {
    const posts = server([ROW]);
    render(<OfflineReview timezone="Asia/Baku" />);
    const card = within(await screen.findByTestId("offline-7"));
    fireEvent.change(screen.getByLabelText("How you verified it"), { target: { value: "Host's paper list" } });
    fireEvent.click(card.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Enter the time you verified");
    expect(posts).toEqual([]);
    fireEvent.change(card.getByLabelText("Verified time (HH:MM)"), { target: { value: "15:52" } });
    fireEvent.click(card.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]?.body).toEqual({ reason: "Host's paper list", verified_at: "2026-11-13T15:52:00+04:00" });
    expect(await screen.findByText("Not applied: the attendee's card was replaced or revoked.")).toBeInTheDocument();
  });

  it("offers no decision on the reviewer's own scan", async () => {
    server([{ ...ROW, own_scan: true }]);
    render(<OfflineReview timezone="Asia/Baku" />);
    const card = within(await screen.findByTestId("offline-7"));
    expect(card.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(card.getByText("You made this scan; another Game Master must review it.")).toBeInTheDocument();
  });

  it("asks for the attendee's card again on a scan saved before card checks", async () => {
    const posts = server([{ ...ROW, card_evidence: false }]);
    render(<OfflineReview timezone="Asia/Baku" />);
    const card = within(await screen.findByTestId("offline-7"));
    fireEvent.change(screen.getByLabelText("How you verified it"), { target: { value: "Paper list" } });
    fireEvent.change(card.getByLabelText("Verified time (HH:MM)"), { target: { value: "15:52" } });
    fireEvent.click(card.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("status")).toHaveTextContent("scan the attendee's current card");
    expect(posts).toEqual([]);
    fireEvent.change(card.getByLabelText("Attendee's current card code (re-scan)"), { target: { value: "C".repeat(43) } });
    fireEvent.click(card.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]?.body.card_token).toBe("C".repeat(43));
  });
});
