// The Beyond Now interest card: four questions with a fixed list of answers.
// Answers travel and are stored as stable identifiers; the label is only what
// is shown (English here, Azerbaijani and Russian in i18n-card.ts). The server
// accepts exactly these identifiers (backend/app/modules/attendees/
// interest_card.py; a backend test compares the two lists) and re-checks every
// rule below - these are for immediate feedback only.
//
// The documents set maximums only, so no group is required.

export type CardOption = { id: string; label: string };

export const SECTORS = [
  { id: "bank_finance", label: "Banking and finance" },
  { id: "telecom_it", label: "Telecom and IT" },
  { id: "public_sector", label: "Public sector" },
  { id: "education", label: "Education" },
  { id: "healthcare", label: "Healthcare" },
  { id: "retail", label: "Retail" },
  { id: "manufacturing_logistics", label: "Manufacturing and logistics" },
  { id: "energy", label: "Energy" },
  { id: "media_marketing", label: "Media and marketing" },
  { id: "hr_consulting", label: "HR and consulting" },
  { id: "startup", label: "Startup" },
  { id: "student", label: "Student" },
  { id: "other", label: "Other" },
] as const;

export const TOPICS = [
  { id: "ai_solutions", label: "AI solutions" },
  { id: "business_solutions", label: "Business solutions" },
  { id: "erp", label: "ERP" },
  { id: "data", label: "Data" },
  { id: "ai_education", label: "AI and education" },
  { id: "hr", label: "HR" },
  { id: "design", label: "Design" },
  { id: "personal_effectiveness", label: "Personal effectiveness" },
  { id: "ai_strategy", label: "AI strategy" },
  { id: "fintech", label: "Fintech" },
  { id: "marketing", label: "Marketing" },
  { id: "cybersecurity", label: "Cybersecurity" },
  { id: "startups_investment", label: "Startups and investment" },
  { id: "public_services", label: "Public services" },
  { id: "ai_ethics", label: "AI ethics" },
] as const;

export const SEEKING = [
  { id: "solution_supplier", label: "A solution or supplier" },
  { id: "partner", label: "A partner" },
  { id: "investor", label: "An investor" },
  { id: "employee", label: "An employee" },
  { id: "job_opportunity", label: "A job opportunity" },
  { id: "mentor", label: "A mentor" },
  { id: "knowledge_exchange", label: "Knowledge exchange" },
  { id: "friendship", label: "Friendship and new connections" },
  { id: "career_advice", label: "Career advice" },
] as const;

export const OFFERING = [
  { id: "product_service", label: "A product or service" },
  { id: "partnership", label: "Partnership" },
  { id: "investment", label: "Investment" },
  { id: "candidate", label: "I'm a candidate" },
  { id: "vacancy", label: "A vacancy" },
  { id: "mentoring", label: "Mentoring" },
  { id: "sharing_experience", label: "Sharing experience" },
  { id: "career_advice", label: "Career advice" },
  { id: "volunteering", label: "Volunteering" },
  { id: "friendship_hobby", label: "Friendship and shared hobbies" },
] as const;

export type InterestCard = {
  sector: string | null;
  topics: string[];
  seeking: string[];
  offering: string[];
};

export type CardGroupKey = keyof InterestCard;

export type CardGroup = {
  key: CardGroupKey;
  title: string;
  hint: string;
  max: number;
  options: readonly CardOption[];
};

export const CARD_GROUPS: readonly CardGroup[] = [
  { key: "sector", title: "My field", hint: "Choose one.", max: 1, options: SECTORS },
  { key: "topics", title: "Topics I'm interested in", hint: "Choose up to 3.", max: 3, options: TOPICS },
  { key: "seeking", title: "What I'm looking for", hint: "Choose up to 2.", max: 2, options: SEEKING },
  { key: "offering", title: "What I offer", hint: "Choose up to 2.", max: 2, options: OFFERING },
];

export const EMPTY_CARD: InterestCard = { sector: null, topics: [], seeking: [], offering: [] };

export function selectedIn(card: InterestCard, key: CardGroupKey): string[] {
  if (key === "sector") return card.sector ? [card.sector] : [];
  return card[key];
}

// One tap on an answer. A selected answer is always removable. In the
// single-answer group a new answer replaces the old one; elsewhere a new answer
// is ignored once the group is full (its button is disabled then anyway).
export function toggleChoice(card: InterestCard, key: CardGroupKey, id: string): InterestCard {
  if (key === "sector") return { ...card, sector: card.sector === id ? null : id };
  const current = card[key];
  if (current.includes(id)) return { ...card, [key]: current.filter((v) => v !== id) };
  const group = CARD_GROUPS.find((g) => g.key === key);
  if (group && current.length >= group.max) return card;
  return { ...card, [key]: [...current, id] };
}

// What the server sent, made safe to hold: a missing card (an older server) is
// empty; anything that is not a list of strings is dropped. Identifiers the
// catalogue no longer has are KEPT, so an edit never silently removes them.
export function cardFrom(value: unknown): InterestCard {
  if (typeof value !== "object" || value === null) return { ...EMPTY_CARD };
  const raw = value as Record<string, unknown>;
  const list = (v: unknown) =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  return {
    sector: typeof raw.sector === "string" ? raw.sector : null,
    topics: list(raw.topics),
    seeking: list(raw.seeking),
    offering: list(raw.offering),
  };
}

export function sameCard(a: InterestCard, b: InterestCard): boolean {
  const same = (x: string[], y: string[]) =>
    x.length === y.length && [...x].sort().every((v, i) => v === [...y].sort()[i]);
  return (
    a.sector === b.sector && same(a.topics, b.topics) && same(a.seeking, b.seeking) &&
    same(a.offering, b.offering)
  );
}

// Fast feedback for the same rules the server enforces: at most `max` answers,
// each from the list, none twice. `saved` is the card as saved (profile editing):
// an answer retired from the list may stay if it is saved in that same group -
// it still counts towards the maximum, and nothing else unknown is accepted.
// Returns the problem as text for `t()`, or null.
export function cardProblem(card: InterestCard, saved?: InterestCard): string | null {
  for (const group of CARD_GROUPS) {
    const chosen = selectedIn(card, group.key);
    const allowed = new Set([
      ...group.options.map((o) => o.id),
      ...(saved ? selectedIn(saved, group.key) : []),
    ]);
    if (
      chosen.length > group.max ||
      new Set(chosen).size !== chosen.length ||
      chosen.some((id) => !allowed.has(id))
    ) {
      return "Check your interest card choices.";
    }
  }
  return null;
}

// The label of an answer, for display. An identifier the catalogue no longer
// has is shown as-is rather than hidden.
export function labelOf(key: CardGroupKey, id: string): string {
  const group = CARD_GROUPS.find((g) => g.key === key);
  return group?.options.find((o) => o.id === id)?.label ?? id;
}

export const MAX_COMPANY_LENGTH = 200;
