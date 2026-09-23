// ARCHITECTURE.md §20.4: "The frontend maps code → copy via a single
// lookup, with a generic fallback for unrecognised codes so an older
// client degrades gracefully instead of rendering nothing."

const MESSAGES: Record<string, string> = {
  VALIDATION_ERROR: "Some details need attention — please check the highlighted fields.",
  UNKNOWN_INTEREST:
    "One of the selected interests is no longer available. Please reload the page and choose again.",
  // One message on purpose: the server deliberately does not say
  // whether a code was wrong, expired, already used or locked (§20.4),
  // so the UI must not pretend to know either.
  OTP_INVALID:
    "That code didn't work. It may be incorrect, expired, already used, or locked after too many attempts. Check it, or request a new code.",
  // §9.4, verbatim: one message for every invalid-token cause.
  INVALID_QR: "This QR code isn't valid. Ask the attendee to reopen their card.",
  BOOTH_NOT_FOUND: "This booth couldn't be found. It may have been removed or renamed.",
  MAINTENANCE: "The event system is briefly unavailable. Please try again in a moment.",
  UNAUTHENTICATED: "Your session has ended. Please sign in again.",
  REGISTRATION_NOT_VERIFIED: "Please verify your email to continue.",
  CSRF_REJECTED: "This request couldn't be verified. Please reload the page and try again.",
  RATE_LIMITED: "Too many attempts. Please wait a moment and try again.",
  INTERNAL_ERROR: "Something went wrong on our side. Please try again.",
  NETWORK_ERROR: "We couldn't reach the server. Check your connection and try again.",
};

export const FALLBACK_MESSAGE = "Something went wrong. Please try again.";

export function messageFor(code: string): string {
  return MESSAGES[code] ?? FALLBACK_MESSAGE;
}

const FIELD_MESSAGES: Record<string, string> = {
  first_name: "Enter your first name.",
  last_name: "Enter your last name.",
  email: "Enter a valid email address.",
  phone: "Enter your phone number.",
  profession: "Enter your profession or role.",
  linkedin_url: "Enter a full LinkedIn URL, e.g. https://www.linkedin.com/in/your-name",
  interest_slugs: "Choose interests from the list.",
  sponsor_consent: "Choose whether to share your details with sponsors.",
  code: "Enter the 6-digit code from your email.",
};

// Turns a VALIDATION_ERROR's `details.errors[].loc` (e.g. ["body",
// "linkedin_url"]) into per-field copy. Server messages are never shown
// raw — they describe validation rules, not what the attendee should do.
export function fieldErrorsFrom(details: Record<string, unknown>): Record<string, string> {
  const errors = details["errors"];
  if (!Array.isArray(errors)) return {};
  const result: Record<string, string> = {};
  for (const entry of errors) {
    if (typeof entry !== "object" || entry === null) continue;
    const loc = (entry as { loc?: unknown }).loc;
    if (!Array.isArray(loc)) continue;
    const field = loc[1];
    if (typeof field !== "string" || field in result) continue;
    result[field] = FIELD_MESSAGES[field] ?? "Please check this field.";
  }
  return result;
}
