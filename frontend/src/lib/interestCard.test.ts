import { describe, expect, it } from "vitest";

import { hasTranslation, translate } from "./i18n";
import {
  CARD_GROUPS,
  cardFrom,
  cardProblem,
  EMPTY_CARD,
  OFFERING,
  sameCard,
  SECTORS,
  SEEKING,
  TOPICS,
  toggleChoice,
  type InterestCard,
} from "./interestCard";

describe("the catalogue", () => {
  it("has the documented answers as stable, distinct identifiers", () => {
    expect([SECTORS.length, TOPICS.length, SEEKING.length, OFFERING.length]).toEqual([13, 15, 9, 10]);
    for (const list of [SECTORS, TOPICS, SEEKING, OFFERING]) {
      const ids = list.map((o) => o.id);
      expect(new Set(ids).size).toBe(ids.length);
      for (const id of ids) expect(id).toMatch(/^[a-z][a-z_]*$/); // never a display label
    }
  });

  it("has an Azerbaijani and Russian label for every answer, title and hint", () => {
    const texts = CARD_GROUPS.flatMap((g) => [g.title, g.hint, ...g.options.map((o) => o.label)]);
    for (const text of texts) expect(hasTranslation(text), text).toBe(true);
    expect(translate("Banking and finance", "az")).toBe("Bank və maliyyə");
    expect(translate("Topics I'm interested in", "az")).toBe("Maraq mövzularım");
    expect(translate("What I offer", "ru")).toBe("Что я предлагаю");
  });
});

describe("toggleChoice", () => {
  it("swaps the single field answer and can clear it", () => {
    let card = toggleChoice(EMPTY_CARD, "sector", "energy");
    expect(card.sector).toBe("energy");
    card = toggleChoice(card, "sector", "education");
    expect(card.sector).toBe("education");
    expect(toggleChoice(card, "sector", "education").sector).toBeNull();
  });

  it("stops at the maximum but always lets a chosen answer go", () => {
    let card: InterestCard = EMPTY_CARD;
    for (const id of ["data", "erp", "hr"]) card = toggleChoice(card, "topics", id);
    expect(card.topics).toEqual(["data", "erp", "hr"]);
    expect(toggleChoice(card, "topics", "design").topics).toEqual(["data", "erp", "hr"]);
    card = toggleChoice(card, "topics", "erp");
    expect(card.topics).toEqual(["data", "hr"]);
    expect(toggleChoice(card, "topics", "design").topics).toEqual(["data", "hr", "design"]);

    let seeking: InterestCard = EMPTY_CARD;
    for (const id of ["partner", "mentor", "investor"]) seeking = toggleChoice(seeking, "seeking", id);
    expect(seeking.seeking).toEqual(["partner", "mentor"]);
    let offering: InterestCard = EMPTY_CARD;
    for (const id of ["vacancy", "mentoring", "investment"]) offering = toggleChoice(offering, "offering", id);
    expect(offering.offering).toEqual(["vacancy", "mentoring"]);
  });
});

describe("cardProblem (the rules the server enforces)", () => {
  it("accepts an empty card and a full one", () => {
    expect(cardProblem(EMPTY_CARD)).toBeNull();
    expect(
      cardProblem({ sector: "student", topics: ["data", "erp", "hr"], seeking: ["partner", "mentor"], offering: ["mentoring", "vacancy"] }),
    ).toBeNull();
  });

  it.each<InterestCard>([
    { ...EMPTY_CARD, topics: ["data", "erp", "hr", "design"] },
    { ...EMPTY_CARD, seeking: ["partner", "partner"] },
    { ...EMPTY_CARD, offering: ["unknown"] },
    { ...EMPTY_CARD, sector: "Bank və maliyyə" },
    { ...EMPTY_CARD, topics: ["friendship"] },
  ])("refuses %o", (card) => {
    expect(cardProblem(card)).toBe("Check your interest card choices.");
  });

  it("lets a saved answer the list no longer has stay, in its own group only", () => {
    const saved: InterestCard = { ...EMPTY_CARD, sector: "old_sector", topics: ["data", "old_topic"] };
    expect(cardProblem(saved, saved)).toBeNull(); // kept as it is
    expect(cardProblem({ ...saved, topics: ["data"] }, saved)).toBeNull(); // removed
    expect(cardProblem(saved)).toBe("Check your interest card choices."); // registration: nothing saved
    for (const card of [
      { ...saved, topics: ["old_topic", "new_unknown"] }, // nothing new and unknown
      { ...saved, topics: ["data"], seeking: ["old_topic"] }, // not moved to another group
      { ...saved, topics: ["data", "old_topic", "erp", "hr"] }, // still counts towards the maximum
      { ...saved, topics: ["old_topic", "old_topic"] },
    ]) {
      expect(cardProblem(card, saved)).toBe("Check your interest card choices.");
    }
  });
});

describe("cardFrom / sameCard", () => {
  it("reads an older server's missing card as empty and keeps stored values", () => {
    expect(cardFrom(undefined)).toEqual(EMPTY_CARD);
    expect(cardFrom({ sector: "energy", topics: ["data", 7], seeking: "x" })).toEqual({
      sector: "energy",
      topics: ["data"],
      seeking: [],
      offering: [],
    });
  });

  it("ignores order but not content", () => {
    const a = { ...EMPTY_CARD, topics: ["data", "erp"] };
    expect(sameCard(a, { ...EMPTY_CARD, topics: ["erp", "data"] })).toBe(true);
    expect(sameCard(a, { ...EMPTY_CARD, topics: ["data"] })).toBe(false);
    expect(sameCard(a, { ...a, sector: "energy" })).toBe(false);
  });
});
