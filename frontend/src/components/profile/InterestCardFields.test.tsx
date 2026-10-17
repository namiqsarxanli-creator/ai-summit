import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { EMPTY_CARD, type InterestCard } from "@/lib/interestCard";

import { InterestCardFields } from "./InterestCardFields";

function Harness({ initial = EMPTY_CARD }: { initial?: InterestCard }) {
  const [card, setCard] = useState(initial);
  return (
    <>
      <InterestCardFields idPrefix="t" card={card} onChange={setCard} />
      <output data-testid="value">{JSON.stringify(card)}</output>
    </>
  );
}

const value = () => JSON.parse(screen.getByTestId("value").textContent ?? "{}") as InterestCard;
const group = (title: string) => screen.getByRole("group", { name: title });

describe("InterestCardFields", () => {
  it("shows the four questions with every answer", () => {
    render(<Harness />);
    for (const title of ["My field", "Topics I'm interested in", "What I'm looking for", "What I offer"]) {
      expect(group(title)).toBeInTheDocument();
    }
    expect(within(group("My field")).getAllByRole("checkbox")).toHaveLength(13);
    expect(within(group("Topics I'm interested in")).getAllByRole("checkbox")).toHaveLength(15);
    expect(within(group("What I'm looking for")).getAllByRole("checkbox")).toHaveLength(9);
    expect(within(group("What I offer")).getAllByRole("checkbox")).toHaveLength(10);
  });

  it("disables the rest of a full group and keeps the chosen ones removable", () => {
    render(<Harness />);
    const topics = within(group("Topics I'm interested in"));
    for (const label of ["Data", "ERP", "Fintech"]) fireEvent.click(topics.getByLabelText(label));
    expect(value().topics).toEqual(["data", "erp", "fintech"]);
    expect(topics.getByLabelText("Design")).toBeDisabled();
    expect(topics.getByLabelText("Data")).not.toBeDisabled();
    expect(topics.getByText("Limit reached — remove a choice to pick another.")).toBeInTheDocument();
    expect(topics.getByText(/3\/3/)).toBeInTheDocument();

    fireEvent.click(topics.getByLabelText("ERP")); // remove at the limit
    expect(value().topics).toEqual(["data", "fintech"]);
    expect(topics.getByLabelText("Design")).not.toBeDisabled();
  });

  it("keeps the same answer separate in two groups (Career advice)", () => {
    render(<Harness />);
    fireEvent.click(within(group("What I'm looking for")).getByLabelText("Career advice"));
    expect(value()).toMatchObject({ seeking: ["career_advice"], offering: [] });
    fireEvent.click(within(group("What I offer")).getByLabelText("Career advice"));
    expect(value()).toMatchObject({ seeking: ["career_advice"], offering: ["career_advice"] });
  });

  it("swaps the single field answer instead of blocking", () => {
    render(<Harness />);
    const field = within(group("My field"));
    fireEvent.click(field.getByLabelText("Energy"));
    fireEvent.click(field.getByLabelText("Student"));
    expect(value().sector).toBe("student");
    expect(field.getByLabelText("Energy")).not.toBeDisabled();
    fireEvent.click(field.getByLabelText("Student"));
    expect(value().sector).toBeNull();
  });

  it("shows a stored answer the list no longer has, marked, so it can be removed but not re-added", () => {
    render(<Harness initial={{ ...EMPTY_CARD, topics: ["retired_topic"] }} />);
    const topics = within(group("Topics I'm interested in"));
    const retired = topics.getByLabelText(/retired_topic/);
    expect(retired.closest("label")).toHaveTextContent("no longer offered");
    fireEvent.click(retired);
    expect(value().topics).toEqual([]);
    expect(topics.queryByLabelText(/retired_topic/)).toBeNull(); // gone: nothing to pick again
  });
});
