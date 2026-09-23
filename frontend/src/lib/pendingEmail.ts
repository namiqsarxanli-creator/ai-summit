// Carries the just-registered address from /register to /verify.
// sessionStorage, not the URL: an email address never belongs in a
// query string (it would land in history, logs and referrers).
const KEY = "facemark.pendingEmail";

export function savePendingEmail(email: string): void {
  try {
    window.sessionStorage.setItem(KEY, email);
  } catch {
    // Storage unavailable (private mode, blocked) — /verify still has
    // its own email field, so the flow degrades rather than breaks.
  }
}

export function readPendingEmail(): string {
  try {
    return window.sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function clearPendingEmail(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}
