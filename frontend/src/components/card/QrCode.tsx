import { useMemo } from "react";

import { qrMatrix, qrPath } from "@/lib/qr";

// The standard 4-module quiet zone scanners need around the code.
const QUIET_ZONE = 4;

// Dark on white regardless of the page theme: phone cameras and QR
// decoders expect that polarity, and the white margin is the quiet zone.
export function QrCode({ value, label }: { value: string; label: string }) {
  const matrix = useMemo(() => qrMatrix(value), [value]);
  const dim = matrix.size + QUIET_ZONE * 2;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${dim} ${dim}`}
      shapeRendering="crispEdges"
      className="block h-auto w-full"
    >
      <rect width={dim} height={dim} fill="#ffffff" />
      <path d={qrPath(matrix, QUIET_ZONE)} fill="#000000" />
    </svg>
  );
}
