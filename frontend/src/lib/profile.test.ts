import { afterEach, describe, expect, it, vi } from "vitest";

import {
  diffDrafts,
  draftOf,
  fetchProfile,
  MAX_PHOTO_BYTES,
  normalizedDraft,
  parseProfile,
  photoProblem,
  removePhoto,
  sameDraft,
  setSponsorConsent,
  updateProfile,
  uploadPhoto,
  validateProfile,
  type AttendeeProfile,
} from "./profile";

const KEY = "0123456789abcdef0123456789abcdef";

const PROFILE: AttendeeProfile = {
  first_name: "Ada",
  last_name: "Lovelace",
  email: "ada@example.com",
  phone: "+1-555-0142",
  profession: "Systems Engineer",
  linkedin_url: "https://www.linkedin.com/in/ada-demo",
  sponsor_consent: true,
  sponsor_consent_at: "2026-10-01T10:00:00Z",
  sponsor_consent_version: "registration-v1",
  interests: [
    { slug: "generative-ai", name: "Generative AI" },
    { slug: "data-analytics", name: "Data Analytics" },
  ],
  company: "AIWorks",
  interest_card: { sector: "bank_finance", topics: ["data"], seeking: [], offering: ["mentoring"] },
  version: 3,
  photo: null,
};

function stubFetch(status = 200, body: unknown = {}) {
  const mock = vi.fn(
    async () =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

function lastCall(mock: ReturnType<typeof stubFetch>): [string, RequestInit] {
  return mock.mock.calls.at(-1) as unknown as [string, RequestInit];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchProfile", () => {
  it("calls the same-origin private profile endpoint with credentials", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchProfile();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/attendees/me/profile");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("same-origin");
  });

  it("treats a photo URL that is not one the server makes as no photo", async () => {
    stubFetch(200, { ...PROFILE, photo: { small: "https://evil.example/a.jpg", large: "x" } });
    const result = await fetchProfile();
    expect(result.ok && result.data.photo).toBeNull();
  });

  it("keeps a real photo and supplies neutral values an older server did not send", async () => {
    const photo = {
      small: `/api/media/avatars/${KEY}-128.jpg`,
      large: `/api/media/avatars/${KEY}-384.jpg`,
    };
    // an older server's profile: the fields added since are simply absent
    const old: Partial<AttendeeProfile> = { ...PROFILE, photo };
    delete old.version;
    delete old.sponsor_consent_at;
    stubFetch(200, old);
    const result = await fetchProfile();
    expect(result.ok && result.data.photo).toEqual(photo);
    expect(result.ok && result.data.version).toBe(1);
    expect(result.ok && result.data.sponsor_consent_at).toBeNull();
  });
});

describe("drafts", () => {
  it("starts from the saved profile, with an empty string for no LinkedIn", () => {
    expect(draftOf({ ...PROFILE, linkedin_url: null })).toMatchObject({ linkedin_url: "" });
    expect(draftOf(PROFILE).interest_slugs).toEqual(["generative-ai", "data-analytics"]);
  });

  it("is unchanged by whitespace and by the order of interests, changed by anything else", () => {
    const base = draftOf(PROFILE);
    expect(sameDraft(base, { ...base, first_name: "  Ada  " })).toBe(true);
    expect(sameDraft(base, { ...base, interest_slugs: [...base.interest_slugs].reverse() })).toBe(true);
    expect(sameDraft(base, { ...base, interest_slugs: ["generative-ai", "generative-ai", "data-analytics"] })).toBe(true);
    expect(sameDraft(base, { ...base, first_name: "Adah" })).toBe(false);
    expect(sameDraft(base, { ...base, phone: "1" })).toBe(false);
    expect(sameDraft(base, { ...base, linkedin_url: "" })).toBe(false);
    expect(sameDraft(base, { ...base, interest_slugs: ["generative-ai"] })).toBe(false);
  });

  it("normalises exactly what would be sent", () => {
    expect(
      normalizedDraft({
        first_name: " A ",
        last_name: "B ",
        phone: " 1",
        profession: " P ",
        linkedin_url: " ",
        interest_slugs: ["b", "a", "b"],
        company: "  Acme  ",
        interest_card: { sector: null, topics: [], seeking: [], offering: [] },
      }),
    ).toEqual({
      first_name: "A",
      last_name: "B",
      phone: "1",
      profession: "P",
      linkedin_url: "",
      interest_slugs: ["a", "b"],
      company: "Acme",
      interest_card: { sector: null, topics: [], seeking: [], offering: [] },
    });
  });
});

describe("validateProfile (the rules registration has)", () => {
  it("accepts a complete draft, with or without LinkedIn", () => {
    expect(validateProfile(draftOf(PROFILE))).toEqual({});
    expect(validateProfile({ ...draftOf(PROFILE), linkedin_url: "" })).toEqual({});
  });

  it("names each missing or malformed field", () => {
    const errors = validateProfile({
      first_name: " ",
      last_name: "",
      phone: "",
      profession: "  ",
      linkedin_url: "https://example.com/in/x",
      interest_slugs: [],
      company: "",
      interest_card: { sector: null, topics: [], seeking: [], offering: [] },
    });
    expect(Object.keys(errors).sort()).toEqual(
      ["first_name", "last_name", "linkedin_url", "phone", "profession"],
    );
  });

  it("has no email or consent rule: neither is editable here", () => {
    const errors = validateProfile({ ...draftOf(PROFILE), first_name: "" });
    expect(errors).not.toHaveProperty("email");
    expect(errors).not.toHaveProperty("sponsor_consent");
  });
});

describe("updateProfile", () => {
  it("sends the whole editable set and the version it was loaded at - and no email", async () => {
    const mock = stubFetch(200, { ...PROFILE, version: 4 });
    const result = await updateProfile(
      { ...draftOf(PROFILE), first_name: "  Grace ", linkedin_url: "" },
      3,
    );
    const [url, init] = lastCall(mock);
    expect(url).toBe("/api/attendees/me/profile");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      first_name: "Grace",
      last_name: "Lovelace",
      phone: "+1-555-0142",
      profession: "Systems Engineer",
      linkedin_url: null,
      interest_slugs: ["data-analytics", "generative-ai"],
      company: "AIWorks",
      interest_card: { sector: "bank_finance", topics: ["data"], seeking: [], offering: ["mentoring"] },
      expected_version: 3,
    });
    expect(result.ok && result.data.version).toBe(4);
  });

  it("returns the server's error untouched, conflict details included", async () => {
    stubFetch(409, { error: { code: "PROFILE_CONFLICT", message: "", details: { profile: PROFILE } } });
    const result = await updateProfile(draftOf(PROFILE), 1);
    expect(!result.ok && result.error.code).toBe("PROFILE_CONFLICT");
    expect(!result.ok && parseProfile(result.error.details["profile"])?.version).toBe(3);
  });
});

describe("parseProfile", () => {
  it("accepts a profile-shaped value and refuses anything else", () => {
    expect(parseProfile(PROFILE)?.first_name).toBe("Ada");
    expect(parseProfile(null)).toBeNull();
    expect(parseProfile("x")).toBeNull();
    expect(parseProfile({ ...PROFILE, version: "3" })).toBeNull();
    expect(parseProfile({ ...PROFILE, interests: null })).toBeNull();
    expect(parseProfile({ first_name: "only" })).toBeNull();
  });
});

describe("diffDrafts", () => {
  it("lists only the fields that differ, with readable interest names", () => {
    const mine = { ...draftOf(PROFILE), profession: "Mine", interest_slugs: ["generative-ai"] };
    const theirs = { ...draftOf(PROFILE), profession: "Theirs" };
    const names: Record<string, string> = { "generative-ai": "Generative AI", "data-analytics": "Data Analytics" };
    const diffs = diffDrafts(mine, theirs, (slug) => names[slug] ?? slug);
    expect(diffs).toEqual([
      { field: "profession", mine: "Mine", theirs: "Theirs" },
      { field: "interest_slugs", mine: "Generative AI", theirs: "Data Analytics, Generative AI" },
    ]);
    expect(diffDrafts(theirs, theirs, (s) => s)).toEqual([]);
  });
});

describe("setSponsorConsent", () => {
  it("sends the choice as a value, to its own endpoint", async () => {
    const mock = stubFetch(200, {
      sponsor_consent: false,
      sponsor_consent_at: null,
      consent_version: null,
      changed: true,
    });
    await setSponsorConsent(false);
    const [url, init] = lastCall(mock);
    expect(url).toBe("/api/attendees/me/sponsor-consent");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ granted: false });
  });
});

describe("photoProblem", () => {
  it("accepts JPEG, PNG and WebP within the size limit", () => {
    for (const type of ["image/jpeg", "image/png", "image/webp"]) {
      expect(photoProblem({ type, size: 1000 })).toBeNull();
    }
    expect(photoProblem({ type: "image/png", size: MAX_PHOTO_BYTES })).toBeNull();
  });

  it("turns away other types, an empty file and an over-size one before any upload", () => {
    for (const type of ["image/svg+xml", "image/gif", "image/heic", "application/pdf", ""]) {
      expect(photoProblem({ type, size: 1000 })).toBe("Choose a JPEG, PNG or WebP picture.");
    }
    expect(photoProblem({ type: "image/png", size: 0 })).toBe("That file is empty. Choose another picture.");
    expect(photoProblem({ type: "image/png", size: MAX_PHOTO_BYTES + 1 })).toBe(
      "That photo is too large. Choose a smaller picture.",
    );
  });
});

describe("photo upload and removal", () => {
  it("sends the picture itself as the raw body - no multipart, no file name", async () => {
    const mock = stubFetch(200, {
      photo: { small: `/api/media/avatars/${KEY}-128.jpg`, large: `/api/media/avatars/${KEY}-384.jpg` },
    });
    const file = new File([new Uint8Array([1, 2, 3])], "../../evil.svg", { type: "image/png" });
    const result = await uploadPhoto(file);
    const [url, init] = lastCall(mock);
    expect(url).toBe("/api/attendees/me/photo");
    expect(init.method).toBe("PUT");
    expect(init.body).toBe(file);
    expect(init.body).not.toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("image/png");
    expect(result.ok && result.data.photo?.small).toContain(KEY);
  });

  it("never trusts a returned URL that the server does not make", async () => {
    stubFetch(200, { photo: { small: "https://evil.example/a.jpg", large: "https://evil.example/b.jpg" } });
    const result = await uploadPhoto(new Blob([new Uint8Array([1])], { type: "image/png" }));
    expect(result.ok && result.data.photo).toBeNull();
  });

  it("removes with DELETE on the same endpoint", async () => {
    const mock = stubFetch(200, { photo: null });
    await removePhoto();
    const [url, init] = lastCall(mock);
    expect(url).toBe("/api/attendees/me/photo");
    expect(init.method).toBe("DELETE");
  });
});
