// Staff scan-resolve (ARCHITECTURE.md §13.1 step 4, §13.3). A DIFFERENT
// shape from `lib/card.ts`'s `PublicCard`/`OwnCard` — the staff response
// is deliberately the minimum needed to confirm identity (§24.20), plus
// the opaque `scan_ref` envelope the (future, M11) award call will echo.

import { apiPost, type ApiResult } from "./api";
import { extractCardToken } from "./card";

const BARE_CARD_TOKEN = /^[A-Za-z0-9_-]{43}$/;

// What the camera decodes is a full card URL (§9.2). The manual-entry
// emergency fallback (§13.5, §24.21) may also be typed as the bare
// token. Anything else — an arbitrary external QR, a mistyped value —
// resolves to null and never reaches the network: it follows the same
// generic INVALID_QR path a bad camera read would, never a distinct
// client-side guess.
export function tokenFromScannedText(text: string): string | null {
  const fromUrl = extractCardToken(text);
  if (fromUrl) return fromUrl;
  const trimmed = text.trim();
  return BARE_CARD_TOKEN.test(trimmed) ? trimmed : null;
}

export type ScanResolveResult = {
  first_name: string;
  last_name: string;
  profession: string;
  // Opaque. §13.3's frontend contract: store it, echo it, never parse
  // it, never inspect it, never track its expiry with a timer.
  scan_ref: string;
};

export function resolveScan(cardToken: string): Promise<ApiResult<ScanResolveResult>> {
  return apiPost<ScanResolveResult>("/api/staff/scan/resolve", { card_token: cardToken });
}
