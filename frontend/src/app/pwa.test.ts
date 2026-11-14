import { readFileSync } from "node:fs";
import { join } from "node:path";
import { webcrypto } from "node:crypto";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";
import { qrMatrix, qrPath } from "@/lib/qr";
import { decodeQrSvg } from "@/test-utils/qr";

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

  it("encrypts the optional offline QR and rejects a wrong PIN", async () => {
    const values = new Map<string, string>();
    const localStorage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    };
    const window = { localStorage } as { localStorage: typeof localStorage; FacemarkOfflineQr?: {
      save: (pin: string, drawing: { dim: number; path: string }) => Promise<void>;
      unlock: (pin: string) => Promise<{ dim: number; path: string }>;
      hasCopy: () => boolean;
      clear: () => void;
    } };
    runInNewContext(publicFile("offline-qr.js").toString("utf8"), {
      window, localStorage, crypto: webcrypto, TextEncoder, TextDecoder,
      btoa: (value: string) => Buffer.from(value, "binary").toString("base64"),
      atob: (value: string) => Buffer.from(value, "base64").toString("binary"),
    });
    const store = window.FacemarkOfflineQr!;
    const cardUrl = "https://facemark.example/c/GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";
    const matrix = qrMatrix(cardUrl);
    const drawing = { dim: matrix.size + 8, path: qrPath(matrix, 4) };
    await store.save("123456", drawing);
    expect(store.hasCopy()).toBe(true);
    expect(values.get("facemark.offlineQr.v1")).not.toContain(drawing.path);
    await expect(store.unlock("000000")).rejects.toThrow();
    expect(await store.unlock("123456")).toEqual(drawing);
    const unlocked = await store.unlock("123456");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${unlocked.dim} ${unlocked.dim}`);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", unlocked.path);
    svg.appendChild(path);
    expect(decodeQrSvg(svg)).toBe(cardUrl);
    store.clear();
    expect(store.hasCopy()).toBe(false);
  });
  it("keeps API and navigated business pages out of offline storage", () => {
    const worker = publicFile("sw.js").toString("utf8");
    expect(worker).toContain('request.mode === "navigate"');
    expect(worker).toContain('url.pathname.startsWith("/_next/static/")');
    expect(worker).not.toMatch(/cache\.put\([^\n]*\/api/);
    expect(publicFile("offline.html").toString("utf8")).toContain("İnternet bağlantısı yoxdur");
    expect(worker).toContain('/offline-qr.js');
  });
});
