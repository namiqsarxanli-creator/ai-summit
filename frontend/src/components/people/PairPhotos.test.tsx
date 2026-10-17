import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PairPhotos } from "./PairPhotos";
import { PhotoFeed } from "./PhotoFeed";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const KEY = "0123456789abcdef0123456789abcdef";
const PHOTO = {
  id: 5, status: "pending", role: "partner", with: "Aysel M.",
  photo_url: `/api/media/pair/${KEY}-384.jpg`, thumb_url: `/api/media/pair/${KEY}-128.jpg`,
  my_screen_consent: false, their_screen_consent: true, moderation: "pending", on_screen: false,
};

afterEach(() => vi.unstubAllGlobals());

describe("PairPhotos", () => {
  it("lets the partner accept, and shares a new photo only with a chosen connection", async () => {
    const posts: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push({ url, body: init.body });
        return json(200, { ...PHOTO, status: "accepted" });
      }
      if (url.endsWith("/connections")) return json(200, { connections: [{ user_id: 9, name: "Aysel M.", profession: "", kind: "other", points: 1, contact: { type: "EMAIL", value: "a@x.az" } }] });
      return json(200, { photos: [PHOTO] });
    }));
    render(<PairPhotos />);
    const card = within(await screen.findByTestId("pair-5"));
    expect(card.getByText("Waiting for your answer")).toBeInTheDocument();
    fireEvent.click(card.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(["/api/attendees/me/pair-photos/5/answer"]));
    // No photo chosen yet: nothing is sent.
    fireEvent.change(screen.getByLabelText("Who is in the photo with you"), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "Share the photo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a photo first.");
    expect(posts).toHaveLength(1);
  });

  it("asks for a connection before any photo can be shared", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url.endsWith("/connections") ? json(200, { connections: [] }) : json(200, { photos: [] })));
    render(<PairPhotos />);
    expect(await screen.findByText("Connect with someone first: scan each other's QR.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Share the photo" })).toBeNull();
  });
});

describe("PhotoFeed", () => {
  it("shows only server-made photo URLs and no names", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { photos: [
      { photo_url: `/api/media/pair/${KEY}-384.jpg` },
      { photo_url: "https://evil.example/x.jpg" },
    ] })));
    const { container } = render(<PhotoFeed />);
    await waitFor(() => expect(container.querySelectorAll("img")).toHaveLength(1));
    expect(container.querySelector("img")?.getAttribute("src")).toContain(KEY);
  });
});
