import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PublicCard } from "@/lib/card";

import { CardView } from "./CardView";

const BASE = {
  first_name: "Grace",
  last_name: "Hopper",
  profession: "Systems Engineer",
  interests: [
    { slug: "generative-ai", name: "Generative AI" },
    { slug: "data-analytics", name: "Data Analytics" },
  ],
};

describe("CardView", () => {
  it("renders the LinkedIn action without leaking the page URL as a referrer", () => {
    const card: PublicCard = {
      ...BASE,
      contact: { type: "LINKEDIN", value: "https://www.linkedin.com/in/grace" },
    };
    render(<CardView card={card} />);
    expect(screen.getByRole("heading", { level: 1, name: "Grace Hopper" })).toBeInTheDocument();
    expect(screen.getByText("Systems Engineer")).toBeInTheDocument();
    expect(screen.getByText("Generative AI")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Connect on LinkedIn" });
    expect(link).toHaveAttribute("href", "https://www.linkedin.com/in/grace");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noreferrer"));
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.queryByRole("link", { name: /email/i })).not.toBeInTheDocument();
  });

  it("renders the email fallback as a mailto action", () => {
    const card: PublicCard = { ...BASE, contact: { type: "EMAIL", value: "grace@example.com" } };
    render(<CardView card={card} />);
    expect(screen.getByRole("link", { name: "Email Grace" })).toHaveAttribute(
      "href",
      "mailto:grace@example.com",
    );
    expect(screen.getByText("grace@example.com")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /linkedin/i })).not.toBeInTheDocument();
  });

  it("never turns a non-http LinkedIn value into a link", () => {
    const card: PublicCard = { ...BASE, contact: { type: "LINKEDIN", value: "javascript:alert(1)" } };
    const { container } = render(<CardView card={card} />);
    expect(container.querySelector("a")).toBeNull();
  });

  it("has nowhere to show a phone number or consent state", () => {
    const card = {
      ...BASE,
      contact: { type: "EMAIL", value: "grace@example.com" },
      // Even if a future server bug sent these, the view does not read them.
      phone: "+1-555-0142",
      sponsor_consent: true,
    } as PublicCard;
    const { container } = render(<CardView card={card} />);
    expect(container.textContent).not.toContain("555");
    expect(container.textContent?.toLowerCase()).not.toContain("consent");
  });
});
