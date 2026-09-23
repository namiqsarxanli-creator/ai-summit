import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { decodeQrSvg } from "@/test-utils/qr";

import { QrCode } from "./QrCode";

const CARD_URL = "http://localhost:3000/c/YGNasg-eVR42rUp_bKbockSk2RLE26A2g9bfCUHvXj8";

describe("QrCode", () => {
  it("draws a code that a scanner decodes to exactly the card URL", () => {
    render(<QrCode value={CARD_URL} label="Your QR" />);
    const svg = screen.getByRole("img", { name: "Your QR" });
    expect(decodeQrSvg(svg)).toBe(CARD_URL);
  });

  it("is dark on white with a quiet zone, whatever the page theme", () => {
    render(<QrCode value={CARD_URL} label="Your QR" />);
    const svg = screen.getByRole("img", { name: "Your QR" });
    const [, , width] = (svg.getAttribute("viewBox") ?? "").split(" ").map(Number);
    expect(svg.querySelector("rect")).toHaveAttribute("fill", "#ffffff");
    expect(svg.querySelector("path")).toHaveAttribute("fill", "#000000");
    expect(svg.querySelector("path")?.getAttribute("d")).not.toMatch(/M[0-3] /);
    expect(width).toBeGreaterThan(21 + 8);
  });

  it("does not also print the URL into the page", () => {
    const { container } = render(<QrCode value={CARD_URL} label="Your QR" />);
    expect(container.innerHTML).not.toContain("YGNasg");
  });
});
