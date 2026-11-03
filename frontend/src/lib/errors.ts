// ARCHITECTURE.md §20.4: "The frontend maps code → copy via a single
// lookup, with a generic fallback for unrecognised codes so an older
// client degrades gracefully instead of rendering nothing."

const MESSAGES: Record<string, string> = {
  SCREEN_LINK_INVALID: "This screen link is not valid. Open the current link from the organiser panel.",
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
  // §13.3/§20.3, verbatim copy: "That scan expired. Scan again."
  SCAN_EXPIRED: "That scan expired. Scan again.",
  IDEMPOTENCY_KEY_REUSE: "This request has already been used for a different award.",
  INVALID_POINT_VALUE: "That point value isn't offered for this booth.",
  AWARD_IN_PROGRESS: "This award is already being processed. Please try again.",
  // §20.3 example message, verbatim.
  DUPLICATE_TASK_REWARD: "This attendee has already earned points for this activity.",
  TASK_NOT_FOUND: "That activity isn't available at your booth. Choose another one.",
  TASK_INACTIVE: "That activity is no longer active. Choose another one.",
  TASK_NOT_CONFIGURED: "That activity has no point value yet. Ask an admin to set one.",
  POINTS_ENTRY_NOT_FOUND: "That points entry no longer exists. Go back to the ledger and refresh it.",
  POINTS_NOT_REVERSIBLE: "Only an award can be reversed. A reversal cannot itself be reversed.",
  POINTS_ALREADY_REVERSED: "This entry has already been reversed. Go back to the ledger and refresh it.",
  CURRENT_PASSWORD_INVALID: "The current password is not correct.",
  PASSWORD_UNCHANGED: "The new password must be different from the current one.",
  PASSWORD_RESET_UNAVAILABLE: "Email is not set up on this server, so no reset email can be sent.",
  EXPORT_TOO_LARGE: "This export is too large. Narrow the filters and try again.",
  EXPORT_BUSY: "An export is already running for your account. Wait for it to finish, then try the download again.",
  EXPORT_TICKET_INVALID: "This export link has expired or was already used. Start the export again.",
  EXPORT_ABORTED: "The server stopped sending the file before the end, so the download is incomplete. Delete the partial file and export again.",
  EXPORT_NOT_STARTED: "The browser did not start the download. Allow downloads for this site, then try again.",
  EXPORT_STATUS_UNKNOWN: "This export's status is no longer available. Export again to be sure.",
  EXPORT_STATUS_UNREACHABLE: "We can't check the export's progress right now. The download may still be running.",
  EXPORT_FAILED: "The export could not be completed. Narrow the filters and try again.",
  TASK_RULE_CHANGED:
    "This activity's points or repeat rule changed. Review it and confirm again.",
  // §12.1, verbatim: never distinguishes unknown email from wrong
  // password (or, as this implementation extends it, a deactivated
  // account with the correct one).
  INVALID_CREDENTIALS: "Incorrect email or password.",
  ACCOUNT_LOCKED: "Too many failed attempts. Please wait 15 minutes and try again.",
  MAINTENANCE: "The event system is briefly unavailable. Please try again in a moment.",
  UNAUTHENTICATED: "Your session has ended. Please sign in again.",
  REGISTRATION_NOT_VERIFIED: "Please verify your email to continue.",
  CSRF_REJECTED: "This request couldn't be verified. Please reload the page and try again.",
  RATE_LIMITED: "Too many attempts. Please wait a moment and try again.",
  INTERNAL_ERROR: "Something went wrong on our side. Please try again.",
  NETWORK_ERROR: "We couldn't reach the server. Check your connection and try again.",
  STAFF_NOT_AUTHORIZED: "This account cannot access that area.",
  ADMIN_INVALID: "Check the values and try again.",
  ADMIN_NOT_FOUND: "That record is no longer available. Refresh and try again.",
  ADMIN_CONFLICT: "That value is already in use. Check the records and try again.",
  // Profile editing: a stale form is never saved over a newer profile.
  PROFILE_CONFLICT: "This profile was changed on another device. Review the latest version below.",
  PHOTO_INVALID:
    "That file isn't a usable photo. Choose a JPEG, PNG or WebP picture that is not animated.",
  PHOTO_TOO_LARGE: "That photo is too large. Choose a smaller picture.",
  PHOTO_BUSY: "Photos are being processed right now. Please try again in a moment.",
  PHOTO_STORAGE_UNAVAILABLE: "Photo storage is unavailable right now. Please try again later.",
  // Masterclass reservations: nothing changed in any of these cases.
  MASTERCLASS_CHOICE_CONFLICT: "Your choice changed on another device. Review the refreshed choices and try again.",
  MASTERCLASS_NOT_FOUND: "This masterclass is not available. Refresh the programme.",
  MASTERCLASS_FULL: "This masterclass is full.",
  MASTERCLASS_WAITLIST_FULL: "There are no places left in this masterclass, not even on the waitlist.",
  MASTERCLASS_CHANGES_CLOSED: "Choices for this block can no longer be changed.",
  MASTERCLASS_SEAT_WOULD_BE_LOST: "This would give up your confirmed seat. Confirm it to continue.",
  MASTERCLASS_ADMIN_CONFLICT: "This change would break existing reservations, so it was not saved.",
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
  company: "Company name is too long.",
  interest_card: "Check your interest card choices.",
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
