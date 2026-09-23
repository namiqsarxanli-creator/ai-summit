// QR encoding for the attendee's card (IMPLEMENTATION_PLAN.md M06: "My
// QR screen rendering the code client-side from the token"). Encoding
// happens in the browser; the only input is the card URL the server
// returned, so nothing else can end up in the code.

import { create } from "qrcode";

export type QrMatrix = {
  size: number;
  isDark: (row: number, col: number) => boolean;
};

// "M" (~15% recovery): dense enough to stay large on a phone screen,
// tolerant enough for glare and a slightly off-angle scan.
export function qrMatrix(text: string): QrMatrix {
  const { modules } = create(text, { errorCorrectionLevel: "M" });
  return { size: modules.size, isDark: (row, col) => modules.get(row, col) === 1 };
}

// Every dark module as its own unit square, so the SVG is exact at any
// size and trivially machine-readable (see qr.test.ts, which decodes it).
export function qrPath(matrix: QrMatrix, quietZone: number): string {
  const parts: string[] = [];
  for (let row = 0; row < matrix.size; row++) {
    for (let col = 0; col < matrix.size; col++) {
      if (matrix.isDark(row, col)) parts.push(`M${col + quietZone} ${row + quietZone}h1v1h-1z`);
    }
  }
  return parts.join("");
}
