// Test-only: decode a QR the way a phone camera would — from pixels —
// so tests assert what the rendered code actually SAYS, not merely that
// an <svg> exists. `encodeToImageData` (M10) goes the other direction:
// it builds the pixels a camera would SEE for a given payload, so the
// scanner's decode pipeline can be tested against a real, standards-
// -conformant QR rather than a hand-rolled fixture.

import jsQR from "jsqr";
import QRCode from "qrcode";

const SCALE = 4;

function rasterize(dim: number, isDark: (x: number, y: number) => boolean) {
  const width = dim * SCALE;
  const pixels = new Uint8ClampedArray(width * width * 4);
  for (let y = 0; y < width; y++) {
    for (let x = 0; x < width; x++) {
      const value = isDark(Math.floor(x / SCALE), Math.floor(y / SCALE)) ? 0 : 255;
      const i = (y * width + x) * 4;
      pixels[i] = pixels[i + 1] = pixels[i + 2] = value;
      pixels[i + 3] = 255;
    }
  }
  return { data: pixels, width, height: width };
}

function decodePixels(dim: number, isDark: (x: number, y: number) => boolean): string | null {
  const { data, width } = rasterize(dim, isDark);
  return jsQR(data, width, width)?.data ?? null;
}

export function encodeToImageData(text: string): {
  data: Uint8ClampedArray;
  width: number;
  height: number;
} {
  const quiet = 4;
  const qr = QRCode.create(text, { errorCorrectionLevel: "M" });
  const size = qr.modules.size;
  return rasterize(size + quiet * 2, (x, y) => {
    const row = y - quiet;
    const col = x - quiet;
    return row >= 0 && col >= 0 && row < size && col < size && qr.modules.get(row, col) === 1;
  });
}

// Reads back the unit squares QrCode draws (`M{x} {y}h1v1h-1z`) inside
// its viewBox, which already includes the quiet zone.
export function decodeQrSvg(svg: Element): string | null {
  const dim = Number(svg.getAttribute("viewBox")?.split(" ")[2]);
  const d = svg.querySelector("path")?.getAttribute("d");
  if (!dim || !d) return null;
  const dark = new Set<string>();
  for (const [, x, y] of d.matchAll(/M(\d+) (\d+)h1v1h-1z/g)) dark.add(`${x},${y}`);
  return decodePixels(dim, (x, y) => dark.has(`${x},${y}`));
}

export function decodeMatrix(size: number, isDark: (row: number, col: number) => boolean): string | null {
  const quiet = 4;
  return decodePixels(size + quiet * 2, (x, y) => {
    const row = y - quiet;
    const col = x - quiet;
    return row >= 0 && col >= 0 && row < size && col < size && isDark(row, col);
  });
}
