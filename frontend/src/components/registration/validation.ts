// Client-side checks that mirror the server's rules (ARCHITECTURE.md
// §8.1) purely for fast feedback. The server remains authoritative:
// everything here is re-validated by the backend, and a client that
// skips these checks gets the same answer as a 422.

import { cardProblem, MAX_COMPANY_LENGTH, type InterestCard } from "@/lib/interestCard";

export type RegistrationValues = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  profession: string;
  linkedin_url: string;
  // Optional (an attendee without a company registers normally).
  company: string;
};

export type ConsentChoice = "yes" | "no" | null;

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isLinkedInUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.hostname.toLowerCase().includes("linkedin.com")
    );
  } catch {
    return false;
  }
}

// The company and interest-card rules registration and profile editing share.
export function companyAndCardErrors(
  company: string,
  card: InterestCard,
  saved?: InterestCard,
): Record<string, string> {
  const errors: Record<string, string> = {};
  // Required (23 Oct 2026): it is how colleagues are told apart.
  if (!company.trim()) errors.company = "Enter your company.";
  else if (company.trim().length > MAX_COMPANY_LENGTH) errors.company = "Company name is too long.";
  const problem = cardProblem(card, saved);
  if (problem) errors.interest_card = problem;
  return errors;
}

export function validateRegistration(
  values: RegistrationValues,
  consent: ConsentChoice,
  card?: InterestCard,
): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!values.first_name.trim()) errors.first_name = "Enter your first name.";
  if (!values.last_name.trim()) errors.last_name = "Enter your last name.";
  if (!EMAIL_SHAPE.test(values.email.trim())) errors.email = "Enter a valid email address.";
  if (!values.phone.trim()) errors.phone = "Enter your phone number.";
  if (!values.profession.trim()) errors.profession = "Enter your profession or role.";
  const linkedin = values.linkedin_url.trim();
  if (linkedin && !isLinkedInUrl(linkedin)) {
    errors.linkedin_url =
      "Enter a full LinkedIn URL, e.g. https://www.linkedin.com/in/your-name";
  }
  // Required explicit choice with no default (§8.1) — nothing is pre-selected.
  if (consent === null) {
    errors.sponsor_consent = "Choose whether to share your details with sponsors.";
  }
  if (card) Object.assign(errors, companyAndCardErrors(values.company, card));
  return errors;
}

export function isSixDigitCode(value: string): boolean {
  return /^\d{6}$/.test(value);
}
