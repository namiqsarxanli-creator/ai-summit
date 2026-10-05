import { describe, expect, it } from "vitest";

import { isAvatarUrl, isLocalPreviewUrl, photoUrlsFrom } from "./avatar";

const KEY = "0123456789abcdef0123456789abcdef";

describe("isAvatarUrl", () => {
  it("accepts exactly the URLs the server makes", () => {
    expect(isAvatarUrl(`/api/media/avatars/${KEY}-128.jpg`)).toBe(true);
    expect(isAvatarUrl(`/api/media/avatars/${KEY}-384.jpg`)).toBe(true);
  });

  it.each([
    null,
    undefined,
    "",
    42,
    {},
    `https://evil.example/api/media/avatars/${KEY}-128.jpg`,
    `//evil.example/api/media/avatars/${KEY}-128.jpg`,
    `/api/media/avatars/${KEY}-999.jpg`,
    `/api/media/avatars/${KEY}-128.png`,
    `/api/media/avatars/${KEY.toUpperCase()}-128.jpg`,
    `/api/media/avatars/${KEY.slice(1)}-128.jpg`,
    `/api/media/avatars/../${KEY}-128.jpg`,
    `/api/media/avatars/${KEY}-128.jpg?x=1`,
    `/api/media/avatars/${KEY}-128.jpg#frag`,
    ` /api/media/avatars/${KEY}-128.jpg`,
    `/api/media/avatars/${KEY}-128.jpg\n`,
    "javascript:alert(1)",
    "data:image/svg+xml,<svg onload=alert(1)>",
    `/api/media/avatars/${KEY}-128.jpg/extra`,
  ])("rejects %j", (value) => {
    expect(isAvatarUrl(value)).toBe(false);
  });
});

describe("isLocalPreviewUrl", () => {
  it("accepts only a blob: URL", () => {
    expect(isLocalPreviewUrl("blob:http://localhost:3000/abc")).toBe(true);
    expect(isLocalPreviewUrl("https://x.example/a.png")).toBe(false);
    expect(isLocalPreviewUrl("data:image/png;base64,AAAA")).toBe(false);
    expect(isLocalPreviewUrl(null)).toBe(false);
  });
});

describe("photoUrlsFrom", () => {
  it("keeps a valid pair and drops anything half-valid", () => {
    const small = `/api/media/avatars/${KEY}-128.jpg`;
    const large = `/api/media/avatars/${KEY}-384.jpg`;
    expect(photoUrlsFrom({ small, large })).toEqual({ small, large });
    expect(photoUrlsFrom({ small, large: "https://evil.example/x.jpg" })).toBeNull();
    expect(photoUrlsFrom({ small })).toBeNull();
    expect(photoUrlsFrom(null)).toBeNull();
    expect(photoUrlsFrom("x")).toBeNull();
  });
});
