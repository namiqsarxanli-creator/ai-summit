// Test-only: decode a QR the way a phone camera would — from pixels —
// so tests assert what the rendered code actually SAYS, not merely that
// an <svg> exists.

import jsQR from "jsqr";

const SCALE = 4;

function decodePixels(dim: number, isDark: (x: number, y: number) => boolean): string | null {
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
  return jsQR(pixels, width, width)?.data ?? null;
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
