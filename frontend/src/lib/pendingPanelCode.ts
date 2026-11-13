// A panel code opened with the phone's own camera in a browser where the attendee is
// not signed in (12 Nov 2026: an iPhone camera opens Safari, the attendee registered in
// another browser). The code is kept in this tab while they sign in, and sent once they
// are back on My points, so one scan is enough. Kept two hours at most; never leaves
// the device; the server still checks the code and the time.

const KEY = "fm-pending-panel-code";
const MAX_AGE_MS = 2 * 60 * 60 * 1000;

export function rememberPanelCode(code: string, now: number = Date.now()): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({ code, at: now }));
  } catch {
    // private mode: the attendee simply scans again after signing in
  }
}

export function hasPendingPanelCode(now: number = Date.now()): boolean {
  return read(now) !== null;
}

/** The waiting code, removed as it is taken (sent at most once). */
export function takePanelCode(now: number = Date.now()): string | null {
  const code = read(now);
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
  return code;
}

function read(now: number): string | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { code?: unknown; at?: unknown };
    if (typeof parsed.code !== "string" || typeof parsed.at !== "number") return null;
    return now - parsed.at <= MAX_AGE_MS ? parsed.code : null;
  } catch {
    return null;
  }
}
