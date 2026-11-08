import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GrandFinalOpen } from "./GrandFinalOpen";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("GrandFinalOpen", () => {
  it("opens the Grand Final code for 15 minutes only after the Game Master confirms", async () => {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(url);
        return json(200, { starts_at: "2026-11-13T13:52:00Z", ends_at: "2026-11-13T14:07:00Z" });
      }
      return json(200, [{ id: 6, title: "Grand Final", kind: "grand_final", starts_at: "2026-11-13T13:30:00Z", ends_at: "2026-11-13T13:45:00Z", timezone: "Asia/Baku" }]);
    }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<GrandFinalOpen />);
    const button = await screen.findByRole("button", { name: "Open the Grand Final code now (15 minutes)" });
    fireEvent.click(button);
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(posts).toEqual([]); // declined: nothing opened
    fireEvent.click(button);
    expect(await screen.findByText(/The Grand Final code is open until/)).toBeInTheDocument();
    expect(posts).toEqual(["/api/staff/grand-final/open"]);
    confirm.mockRestore();
  });
});
