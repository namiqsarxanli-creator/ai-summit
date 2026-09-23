import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Recommendation } from "@/lib/matching";

import { RecommendationCard } from "./RecommendationCard";

describe("RecommendationCard", () => {
  it("renders the score, matched interests and explanation exactly as given", () => {
    const recommendation: Recommendation = {
      booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" },
      match: {
        score: 78,
        matched_interests: [
          { slug: "generative-ai", name: "Generative AI" },
          { slug: "data-analytics", name: "Data Analytics" },
        ],
        explanation: "Good match based on your interests in Generative AI and Data Analytics.",
        algorithm_version: "weighted-v1",
      },
    };
    render(<RecommendationCard recommendation={recommendation} />);
    expect(screen.getByRole("heading", { name: "AIWorks" })).toBeInTheDocument();
    expect(screen.getByText("Hall B · 14")).toBeInTheDocument();
    expect(screen.getByLabelText("78% match")).toHaveTextContent("78%");
    expect(screen.getByText("Generative AI")).toBeInTheDocument();
    expect(screen.getByText("Data Analytics")).toBeInTheDocument();
    expect(
      screen.getByText("Good match based on your interests in Generative AI and Data Analytics."),
    ).toBeInTheDocument();
  });

  it("never renders its own score — only what the server sent", () => {
    // A tampered/mocked prop with an unusual score proves this is a
    // pure render: no recompute, no clamp, no rounding happens here.
    const recommendation: Recommendation = {
      booth: { slug: "x", name: "X Booth", location_label: null },
      match: {
        score: 3,
        matched_interests: [],
        explanation: "Some overlap based on your interests in Robotics.",
        algorithm_version: "weighted-v1",
      },
    };
    render(<RecommendationCard recommendation={recommendation} />);
    expect(screen.getByLabelText("3% match")).toBeInTheDocument();
  });

  it("shows a neutral prompt, never a fabricated percentage, when match is null", () => {
    const recommendation: Recommendation = {
      booth: { slug: "aiworks", name: "AIWorks", location_label: "Hall B · 14" },
      match: null,
    };
    render(<RecommendationCard recommendation={recommendation} />);
    expect(screen.queryByLabelText(/% match/)).not.toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(
      screen.getByText("Select your interests to see how well this booth matches you."),
    ).toBeInTheDocument();
  });

  it("omits the location line when the booth has none", () => {
    const recommendation: Recommendation = {
      booth: { slug: "x", name: "X Booth", location_label: null },
      match: null,
    };
    const { container } = render(<RecommendationCard recommendation={recommendation} />);
    expect(container.textContent).not.toMatch(/null/i);
  });
});
