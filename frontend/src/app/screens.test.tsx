import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { realtimeStore } from "@/lib/realtime";
import { installMockEventSource } from "@/test-utils/mockEventSource";

import BoothDetailPage from "./(attendee)/booths/[slug]/page";
import BoothsPage from "./(attendee)/booths/page";
import LeaderboardPage from "./(attendee)/leaderboard/page";
import LoginPage from "./(attendee)/login/page";
import MyCardPage from "./(attendee)/my-card/page";
import ProfilePage from "./(attendee)/profile/page";
import RegisterPage from "./(attendee)/register/page";
import VerifyPage from "./(attendee)/verify/page";
import PublicCardPage from "./c/[token]/page";
import NotFound from "./not-found";
import Home from "./page";
import StaffLoginPage from "./staff/login/page";
import StaffPage from "./staff/page";
import StaffPasswordPage from "./staff/password/page";
import StaffResetPage from "./staff/reset/page";
import StaffScanPage from "./staff/scan/page";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const TOKEN = "A".repeat(43);

// Every attendee and staff screen, as a user lands on it (signed out: every call is a 401).
const SCREENS: [string, () => ReactElement | Promise<ReactElement>][] = [
  ["home", () => <Home />],
  ["register", () => <RegisterPage />],
  ["login", () => <LoginPage />],
  ["verify", () => <VerifyPage />],
  ["booths", () => <BoothsPage />],
  ["booth detail", () => BoothDetailPage({ params: Promise.resolve({ slug: "nova" }) })],
  ["my card", () => <MyCardPage />],
  ["public card", () => PublicCardPage({ params: Promise.resolve({ token: TOKEN }) })],
  ["leaderboard", () => <LeaderboardPage />],
  ["profile", () => <ProfilePage />],
  ["staff sign in", () => <StaffLoginPage />],
  ["staff panel", () => <StaffPage />],
  ["staff scan", () => <StaffScanPage />],
  ["staff password", () => <StaffPasswordPage />],
  ["staff reset", () => <StaffResetPage />],
  ["not found", () => <NotFound />],
];

beforeEach(() => {
  installMockEventSource();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "", details: {}, correlation_id: "c" } }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
});

afterEach(() => {
  realtimeStore.resetForTests();
  vi.unstubAllGlobals();
});

describe("every attendee and staff screen", () => {
  it.each(SCREENS)("%s credits the maker exactly once and shows no FACEMARK brand title", async (_name, make) => {
    const { container } = render(await make());
    expect(screen.getAllByText("Powered by")).toHaveLength(1);
    expect(screen.getAllByRole("link", { name: "aiworks" })).toHaveLength(1);
    // the brand shown on screen is "AI Summit": no heading or link carries the project name
    for (const element of container.querySelectorAll("h1, h2, header a, .fm-wordmark, .fm-pass-label")) {
      expect(element.textContent).not.toMatch(/FACEMARK/);
    }
  });

  it.each(SCREENS)("%s has one main landmark to skip to", async (_name, make) => {
    render(await make());
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it.each(SCREENS.filter(([name]) => name !== "home" && name !== "booth detail" && name !== "public card"))(
    "%s shows the shared page header with a way back and the AI Summit name",
    async (_name, make) => {
      render(await make());
      if (["booths", "my card", "leaderboard", "profile"].includes(_name)) {
        expect(screen.queryByRole("link", { name: "Back" })).not.toBeInTheDocument();
      } else expect(screen.getByRole("link", { name: "Back" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "AI Summit" })).toHaveAttribute("href", "/");
    },
  );
});
