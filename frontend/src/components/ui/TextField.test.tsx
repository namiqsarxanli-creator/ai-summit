import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TextField } from "./TextField";

describe("TextField", () => {
  it("ties the label, the hint and the input together", () => {
    render(<TextField id="phone" label="Phone" hint="Never shown publicly." />);
    const input = screen.getByLabelText("Phone");
    expect(input).toHaveAccessibleDescription("Never shown publicly.");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("announces an error, marks the input invalid and replaces the hint with it", () => {
    render(<TextField id="phone" label="Phone" hint="Never shown publicly." error="Enter your phone number." />);
    const input = screen.getByLabelText("Phone");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your phone number.");
    expect(input).toHaveAccessibleDescription("Enter your phone number.");
    expect(screen.queryByText("Never shown publicly.")).not.toBeInTheDocument();
  });

  it("passes input attributes through, so a phone keyboard and autofill still work", () => {
    render(<TextField id="phone" label="Phone" type="tel" autoComplete="tel" inputMode="tel" />);
    const input = screen.getByLabelText("Phone");
    expect(input).toHaveAttribute("type", "tel");
    expect(input).toHaveAttribute("autocomplete", "tel");
    expect(input).toHaveAttribute("inputmode", "tel");
  });
});
