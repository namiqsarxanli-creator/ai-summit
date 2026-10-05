// Profile photos are served by the backend at one fixed shape of URL:
//   /api/media/avatars/<32 hex characters>-<128|384>.jpg
// The server makes the name (random, new for every new photo), so the same string
// is also safe to cache for a year. This module is the ONE gate between "a string
// the API sent" and "a src the browser will load": anything else - another
// origin, a data: or javascript: URL, a path with a dot-dot - is treated as "no
// photo", and the screen shows the robot instead.

const AVATAR_URL = /^\/api\/media\/avatars\/[0-9a-f]{32}-(?:128|384)\.jpg$/;

export function isAvatarUrl(value: unknown): value is string {
  return typeof value === "string" && AVATAR_URL.test(value);
}

// A photo picked on this device and not yet uploaded: shown as a preview only.
export function isLocalPreviewUrl(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("blob:");
}

export type PhotoUrls = { small: string; large: string };

// Both URLs or none: a half-valid pair is treated as no photo at all.
export function photoUrlsFrom(value: unknown): PhotoUrls | null {
  if (typeof value !== "object" || value === null) return null;
  const { small, large } = value as { small?: unknown; large?: unknown };
  return isAvatarUrl(small) && isAvatarUrl(large) ? { small, large } : null;
}
