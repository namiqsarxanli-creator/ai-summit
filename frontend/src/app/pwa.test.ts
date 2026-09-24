import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const publicFile = (name: string) => readFileSync(join(process.cwd(), "public", name));

describe("installable static shell", () => {
  it("has a standalone manifest with actual 192 and 512 pixel PNG assets", () => {
    const manifest = JSON.parse(publicFile("manifest.webmanifest").toString("utf8")) as {
      display: string; start_url: string; icons: { src: string; sizes: string }[];
    };
    expect(manifest.display).toBe("standalone");
    expect(manifest.start_url).toBe("/");
    expect(manifest.icons.map((icon) => icon.sizes)).toEqual(["192x192", "512x512"]);
    for (const icon of manifest.icons) {
      const png = publicFile(icon.src.slice(1));
      expect(png.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
    }
  });

  it("keeps API and navigated business pages out of offline storage", () => {
    const worker = publicFile("sw.js").toString("utf8");
    expect(worker).toContain('request.mode === "navigate"');
    expect(worker).toContain('url.pathname.startsWith("/_next/static/")');
    expect(worker).not.toMatch(/cache\.put\([^\n]*\/api/);
    expect(publicFile("offline.html").toString("utf8")).toContain("You're offline");
  });
});
