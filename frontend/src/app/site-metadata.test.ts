import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Metadata } from "next";
import { describe, expect, it } from "vitest";

import { hasTranslation, localiseTitle } from "@/lib/i18n";
import { siteOrigin } from "@/lib/site";

import { metadata as boothsMeta } from "./(attendee)/booths/page";
import { metadata as leaderboardMeta } from "./(attendee)/leaderboard/page";
import { metadata as loginMeta } from "./(attendee)/login/page";
import { metadata as myCardMeta } from "./(attendee)/my-card/page";
import { metadata as profileMeta } from "./(attendee)/profile/page";
import { metadata as registerMeta } from "./(attendee)/register/page";
import { metadata as verifyMeta } from "./(attendee)/verify/page";
import { metadata as adminMeta } from "./admin/layout";
import { metadata as cardMeta } from "./c/[token]/page";
import { metadata as rootMeta } from "./layout";
import { metadata as notFoundMeta } from "./not-found";
import { metadata as staffLoginMeta } from "./staff/login/page";
import { metadata as staffMeta } from "./staff/page";
import { metadata as staffPasswordMeta } from "./staff/password/page";
import { metadata as staffResetMeta } from "./staff/reset/page";
import { metadata as staffScanMeta } from "./staff/scan/page";

const PAGES: [string, Metadata][] = [
  ["booths", boothsMeta], ["leaderboard", leaderboardMeta], ["login", loginMeta],
  ["my card", myCardMeta], ["profile", profileMeta], ["register", registerMeta],
  ["verify", verifyMeta], ["admin", adminMeta], ["public card", cardMeta],
  ["not found", notFoundMeta], ["staff sign in", staffLoginMeta], ["staff panel", staffMeta],
  ["staff password", staffPasswordMeta], ["staff reset", staffResetMeta], ["staff scan", staffScanMeta],
];

const publicFile = (name: string) => readFileSync(join(process.cwd(), "public", name));

function jpegSize(data: Buffer): { width: number; height: number } {
  let i = 2;
  while (i < data.length) {
    const marker = data[i + 1] ?? 0;
    const length = data.readUInt16BE(i + 2);
    if (marker >= 0xc0 && marker <= 0xc3) {
      return { height: data.readUInt16BE(i + 5), width: data.readUInt16BE(i + 7) };
    }
    i += 2 + length;
  }
  throw new Error("no JPEG frame header");
}

describe("what the browser tab and the home screen call the site", () => {
  it.each(PAGES)("%s: the tab says '<page> · AI Summit' in every language, never FACEMARK", (_name, meta) => {
    const page = meta.title as string;
    expect(typeof page).toBe("string");
    expect(page).not.toMatch(/FACEMARK/);
    expect(hasTranslation(page)).toBe(true);
    const full = `${page} · AI Summit`;
    for (const language of ["az", "ru"] as const) {
      const shown = localiseTitle(full, language);
      expect(shown).toMatch(/ · AI Summit$/);
      expect(shown).not.toBe(full); // actually translated
    }
  });

  it("the site's own name is AI Summit in the tab, the installed app and the offline page", () => {
    const title = rootMeta.title as { default: string; template: string };
    expect(title.default).toBe("AI Summit");
    expect(title.template).toBe("%s · AI Summit");
    expect(rootMeta.applicationName).toBe("AI Summit");
    expect(rootMeta.appleWebApp).toMatchObject({ title: "AI Summit" });
    const manifest = JSON.parse(publicFile("manifest.webmanifest").toString("utf8")) as { name: string; short_name: string };
    expect([manifest.name, manifest.short_name]).toEqual(["AI Summit", "AI Summit"]);
    expect(publicFile("offline.html").toString("utf8")).not.toMatch(/FACEMARK/);
  });
});

describe("link previews (WhatsApp, Telegram, LinkedIn...)", () => {
  it("carry a title, a description and a 1200x630 picture at an absolute address", () => {
    expect(rootMeta.metadataBase?.toString()).toBe("https://aisummit.aiworks.az/");
    const og = rootMeta.openGraph as { title: string; description: string; siteName: string; images: { url: string; width: number; height: number }[] };
    expect(og.title).toBe("AI Summit");
    expect(og.siteName).toBe("AI Summit");
    expect(og.description).toMatch(/AI Summit/);
    const image = og.images[0];
    if (!image) throw new Error("no Open Graph image");
    const file = publicFile(image.url.slice(1));
    expect(jpegSize(file)).toEqual({ width: image.width, height: image.height });
    expect([image.width, image.height]).toEqual([1200, 630]);
    expect(file.length).toBeLessThan(300_000); // small enough for messenger previews
    expect(rootMeta.twitter).toMatchObject({ card: "summary_large_image" });
  });

  it("SITE_ORIGIN can point previews at another host, and nonsense falls back to production", () => {
    const saved = process.env.SITE_ORIGIN;
    try {
      process.env.SITE_ORIGIN = "https://staging.example.org/some/path";
      expect(siteOrigin()).toBe("https://staging.example.org");
      process.env.SITE_ORIGIN = "not a url";
      expect(siteOrigin()).toBe("https://aisummit.aiworks.az");
      process.env.SITE_ORIGIN = "javascript:alert(1)";
      expect(siteOrigin()).toBe("https://aisummit.aiworks.az");
    } finally {
      if (saved === undefined) delete process.env.SITE_ORIGIN;
      else process.env.SITE_ORIGIN = saved;
    }
  });
});

describe("the site icon (browser tab, home screen, link previews that show an icon)", () => {
  const png = (name: string) => {
    const data = publicFile(name);
    expect(data.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
    return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
  };

  it("every icon the pages and the manifest name exists, at the size it claims", () => {
    const icons = rootMeta.icons as { icon: { url: string; sizes: string }[]; apple: { url: string; sizes: string } };
    for (const { url, sizes } of [...icons.icon, icons.apple]) {
      if (url.endsWith(".ico")) {
        expect(publicFile(url.slice(1)).readUInt16BE(0)).toBe(0); // ICO header
        continue;
      }
      const [w, h] = sizes.split("x").map(Number);
      expect(png(url.slice(1))).toEqual({ width: w, height: h });
    }
    const manifest = JSON.parse(publicFile("manifest.webmanifest").toString("utf8")) as { icons: { src: string; sizes: string }[] };
    for (const { src, sizes } of manifest.icons) {
      const [w, h] = sizes.split("x").map(Number);
      expect(png(src.slice(1))).toEqual({ width: w, height: h });
    }
  });

  it("the old FACEMARK 'M' icon is gone, so nothing can still point at it", () => {
    for (const old of ["icon-192.png", "icon-512.png", "icon-source.svg"]) {
      expect(() => publicFile(old)).toThrow();
    }
    expect(publicFile("app-icon-source.svg").toString("utf8")).toContain(">AI</text>");
  });
});
