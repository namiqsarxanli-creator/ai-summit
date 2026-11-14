import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AttendeeTabBar } from "./AttendeeTabBar";

describe("AttendeeTabBar", () => {
  it("renders all five tabs from IMPLEMENTATION_PLAN.md M08, in order", () => {
    render(<AttendeeTabBar active="home" />);
    const nav = screen.getByRole("navigation", { name: "Attendee navigation" });
    const links = nav.querySelectorAll("a");
    expect([...links].map((a) => a.textContent)).toEqual([
      "Home",
      "Booths",
      "My QR",
      "Leaderboard",
      "Profile",
    ]);
  });

  it("links each tab to the right route", () => {
    render(<AttendeeTabBar active="home" />);
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Booths" })).toHaveAttribute("href", "/booths");
    expect(screen.getByRole("link", { name: "My QR" })).toHaveAttribute("href", "/my-card");
    expect(screen.getByRole("link", { name: "Leaderboard" })).toHaveAttribute("href", "/leaderboard");
    expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("href", "/profile");
  });

  it("marks exactly the active tab with aria-current", () => {
    render(<AttendeeTabBar active="booths" />);
    expect(screen.getByRole("link", { name: "Booths" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "My QR" })).not.toHaveAttribute("aria-current");
  });

  it("names every tab and marks only the current one", () => {
    render(<AttendeeTabBar active="profile" />);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(5);
    for (const link of links) expect(link).toHaveAccessibleName();
    expect(links.filter((link) => link.getAttribute("aria-current") === "page")).toHaveLength(1);
  });
});
