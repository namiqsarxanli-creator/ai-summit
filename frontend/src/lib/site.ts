// The name and address the site presents to the outside world: browser tabs,
// the installed app, and link previews (Open Graph). "FACEMARK" is the project's
// code name only and is never shown.

export const BRAND = "AI Summit";

export const SITE_DESCRIPTION =
  "Connect with people and discover the booths that match you at AI Summit.";

const DEFAULT_ORIGIN = "https://aisummit.aiworks.az";

/** The public origin link previews point at. Read at build time; SITE_ORIGIN
 * overrides it (a staging host, for example). Anything that is not a valid
 * http(s) URL falls back to the production address rather than failing the build. */
export function siteOrigin(): string {
  const value = process.env.SITE_ORIGIN?.trim();
  if (value) {
    try {
      const url = new URL(value);
      if (url.protocol === "https:" || url.protocol === "http:") return url.origin;
    } catch {
      // fall through to the default
    }
  }
  return DEFAULT_ORIGIN;
}
