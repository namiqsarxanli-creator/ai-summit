// Client-side checks that mirror the server's rules (ARCHITECTURE.md
// §8.1) purely for fast feedback. The server remains authoritative:
// everything here is re-validated by the backend, and a client that
// skips these checks gets the same answer as a 422.

export type RegistrationValues = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  profession: string;
  linkedin_url: string;
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

export function validateRegistration(
  values: RegistrationValues,
  consent: ConsentChoice,
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
  return errors;
}

export function isSixDigitCode(value: string): boolean {
  return /^\d{6}$/.test(value);
}
