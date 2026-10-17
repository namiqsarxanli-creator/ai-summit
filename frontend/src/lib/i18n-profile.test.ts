import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { messageFor } from "./errors";
import { hasTranslation, translate } from "./i18n";
import { PROFILE_TEXTS } from "./i18n-profile";
import { draftOf, photoProblem, validateProfile } from "./profile";

const FILES = [
  "src/components/profile/ProfileView.tsx",
  "src/components/profile/ProfileEditor.tsx",
  "src/components/profile/PhotoControl.tsx",
  "src/components/profile/ConsentControl.tsx",
];

// Every literal the profile screen can put on screen through t(), a field label
// or a hint - found in the source, so a text added later without a translation
// fails here instead of showing English to an Azerbaijani or Russian reader.
function literalsIn(path: string): string[] {
  const source = readFileSync(join(process.cwd(), path), "utf8");
  const found = new Set<string>();
  for (const match of source.matchAll(/\bt\("([^"]+)"\)/g)) found.add(match[1] as string);
  for (const match of source.matchAll(/\b(?:label|hint)="([^"]+)"/g)) found.add(match[1] as string);
  return [...found];
}

describe("profile screen texts", () => {
  it("has an Azerbaijani and a Russian translation for every text it defines", () => {
    for (const text of PROFILE_TEXTS) {
      expect(translate(text, "az"), text).not.toBe(text);
      expect(translate(text, "ru"), text).not.toBe(text);
      expect(translate(text, "az").trim(), text).not.toBe("");
    }
  });

  it("has a translation for every literal the components show", () => {
    const missing: string[] = [];
    for (const file of FILES) {
      for (const text of literalsIn(file)) if (!hasTranslation(text)) missing.push(`${file}: ${text}`);
    }
    expect(missing).toEqual([]);
  });

  it("covers the texts the helpers hand to the screen", () => {
    const texts = [
      ...Object.values(validateProfile({ ...draftOf({ first_name: "", last_name: "", phone: "", profession: "", linkedin_url: "x", sponsor_consent: false, sponsor_consent_at: null, sponsor_consent_version: null, email: "", interests: [], company: "x".repeat(201), interest_card: { sector: "nope", topics: [], seeking: [], offering: [] }, version: 1, photo: null }) })),
      photoProblem({ type: "image/gif", size: 1 }),
      photoProblem({ type: "image/png", size: 0 }),
      photoProblem({ type: "image/png", size: 99 * 1024 * 1024 }),
    ].filter((text): text is string => typeof text === "string");
    expect(texts.length).toBeGreaterThan(4);
    expect(texts.filter((text) => !hasTranslation(text))).toEqual([]);
  });

  it("covers every error the profile, consent and photo calls can answer with", () => {
    const codes = [
      "PROFILE_CONFLICT",
      "PHOTO_INVALID",
      "PHOTO_TOO_LARGE",
      "PHOTO_BUSY",
      "PHOTO_STORAGE_UNAVAILABLE",
      "UNKNOWN_INTEREST",
      "VALIDATION_ERROR",
      "RATE_LIMITED",
      "NETWORK_ERROR",
      "MAINTENANCE",
      "UNAUTHENTICATED",
      "INTERNAL_ERROR",
    ];
    const missing = codes.map(messageFor).filter((text) => !hasTranslation(text));
    expect(missing).toEqual([]);
    expect(messageFor("PROFILE_CONFLICT")).not.toBe(messageFor("DOES_NOT_EXIST"));
  });
});
