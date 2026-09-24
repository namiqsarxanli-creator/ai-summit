// A thin, pure seam around jsQR (M10) — kept separate from the camera
// component so the decode step itself is testable with fabricated pixel
// data, without a real camera, video element or canvas rendering.

import jsQR from "jsqr";

export function decodeQrFromImageData(
  data: Uint8ClampedArray,
  width: number,
  height: number,
): string | null {
  return jsQR(data, width, height)?.data ?? null;
}
