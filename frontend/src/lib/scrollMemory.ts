// Coming back to a page whose content loads after it opens (the home page
// fetches its dashboard), the browser restores the scroll position while the
// page is still short, so the visitor lands at the top. This keeps the position
// a visitor left from (by following a link on the page) and puts them back there
// once the content is in. Per tab (sessionStorage), used once, never required:
// storage that is blocked or full simply means no restore.

const PREFIX = "fm.scroll.";
const SETTLE_MS = 1500; // content below may still grow (a summary loading)

export function rememberScroll(key: string): void {
  try {
    window.sessionStorage.setItem(PREFIX + key, String(Math.round(window.scrollY)));
  } catch {
    /* storage unavailable: nothing to restore later */
  }
}

function takeScroll(key: string): number | null {
  try {
    const raw = window.sessionStorage.getItem(PREFIX + key);
    window.sessionStorage.removeItem(PREFIX + key);
    const y = raw === null ? NaN : Number(raw);
    return Number.isFinite(y) && y > 0 ? y : null;
  } catch {
    return null;
  }
}

// Scrolls back to the remembered position, retrying while the page grows, and
// stops as soon as the visitor scrolls or touches the page themselves.
export function restoreScroll(key: string): () => void {
  const target = takeScroll(key);
  if (target === null) return () => {};
  let stopped = false;
  const stop = () => { stopped = true; };
  const events = ["wheel", "touchstart", "keydown"] as const;
  for (const e of events) window.addEventListener(e, stop, { passive: true, once: true });
  const started = Date.now();
  // The position was measured with every part loaded; parts above it may still be
  // loading now, so it is re-applied until the page settles, not just once.
  const step = () => {
    if (stopped) return;
    if (Math.abs(window.scrollY - target) >= 2) window.scrollTo(0, target);
    if (Date.now() - started < SETTLE_MS) window.requestAnimationFrame(step);
  };
  window.requestAnimationFrame(step);
  return () => {
    stopped = true;
    for (const e of events) window.removeEventListener(e, stop);
  };
}
