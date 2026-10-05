import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Avatar } from "./Avatar";

const KEY = "0123456789abcdef0123456789abcdef";
const SMALL = `/api/media/avatars/${KEY}-128.jpg`;
const OTHER = `/api/media/avatars/${"f".repeat(32)}-128.jpg`;

function circle(container: HTMLElement): HTMLElement {
  return container.querySelector(".fm-avatar") as HTMLElement;
}

describe("Avatar", () => {
  it("shows the photo over the robot when there is one", () => {
    const { container } = render(<Avatar size="rank" url={SMALL} />);
    const image = container.querySelector("img");
    expect(image).not.toBeNull();
    expect(image?.getAttribute("src")).toContain(KEY);
    expect(circle(container)).toHaveAttribute("data-photo", "true");
    expect(circle(container)).toHaveAttribute("aria-hidden", "true"); // decorative: the name is beside it
  });

  it("keeps the leaderboard's own robot circle for the rank size", () => {
    const { container } = render(<Avatar size="rank" url={null} />);
    expect(circle(container)).toHaveClass("fm-rank-avatar");
  });

  it.each([
    ["no photo", null],
    ["no value", undefined],
    ["a foreign origin", "https://evil.example/a.jpg"],
    ["a script URL", "javascript:alert(1)"],
    ["a data URL", "data:image/svg+xml,<svg/>"],
    ["a path that is not an avatar", "/api/attendees/me/profile"],
  ])("shows only the robot for %s", (_label, url) => {
    const { container } = render(<Avatar size="card" url={url} />);
    expect(container.querySelector("img")).toBeNull();
    expect(circle(container)).toHaveAttribute("data-photo", "false");
  });

  it("falls back to the robot when the image fails to load", () => {
    const { container } = render(<Avatar size="profile" url={SMALL} />);
    fireEvent.error(container.querySelector("img") as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    expect(circle(container)).toHaveAttribute("data-photo", "false");
  });

  it("tries a NEW photo URL afresh after an earlier one failed", () => {
    const { container, rerender } = render(<Avatar size="profile" url={SMALL} />);
    fireEvent.error(container.querySelector("img") as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    rerender(<Avatar size="profile" url={OTHER} />);
    expect(container.querySelector("img")).not.toBeNull();
  });

  it("shows a local preview in place of the saved photo, and only for a blob URL", () => {
    const { container, rerender } = render(
      <Avatar size="profile" url={SMALL} previewUrl="blob:http://localhost/preview" />,
    );
    expect(container.querySelector("img")?.getAttribute("src")).toContain("blob:");
    rerender(<Avatar size="profile" url={null} previewUrl="https://evil.example/x.jpg" />);
    expect(container.querySelector("img")).toBeNull();
  });
});
