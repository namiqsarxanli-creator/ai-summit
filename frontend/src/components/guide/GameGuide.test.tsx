import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { hasTranslation } from "@/lib/i18n";

import { GameGuide, GUIDE_TEXTS } from "./GameGuide";

const BOOTHS = [
  { slug: "global-academy-prompt-master", name: "Global Academy — AI Prompt Master Challenge", location_label: null, description: "Hands-on challenges." },
  { slug: "aiworks", name: "AIWorks — Spin & Play", location_label: null, description: null },
];

describe("GameGuide", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("has every text in Azerbaijani and Russian", () => {
    expect(GUIDE_TEXTS.filter((text) => !hasTranslation(text))).toEqual([]);
  });

  it("adds up to the 100 points it promises, and lists the server's booths with a way to book", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(BOOTHS), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<GameGuide />);
    const section = screen.getByRole("region", { name: "How 100 points add up" });
    const points = [...section.querySelectorAll(".fm-badge")].map((n) => Number(n.textContent));
    expect(points.reduce((a, b) => a + b, 0)).toBe(100);
    expect(await screen.findByRole("heading", { name: "Global Academy" })).toBeInTheDocument();
    expect(screen.getByText("AI Prompt Master Challenge")).toBeInTheDocument();
    expect(screen.getByText(/three-stage challenge/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "Book a 15-minute session" }).map((a) => a.getAttribute("href"));
    expect(links).toContain("/playground?booth=aiworks");
  });
});
