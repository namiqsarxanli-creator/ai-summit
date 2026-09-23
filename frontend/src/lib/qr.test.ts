import { describe, expect, it } from "vitest";

import { decodeMatrix } from "@/test-utils/qr";

import { qrMatrix, qrPath } from "./qr";

const CARD_URL = "https://summit.example/c/GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";

describe("qrMatrix", () => {
  it("encodes exactly the given text — decoded, it reads back byte for byte", () => {
    const matrix = qrMatrix(CARD_URL);
    expect(decodeMatrix(matrix.size, matrix.isDark)).toBe(CARD_URL);
  });

  it("is deterministic, so the same card always draws the same code", () => {
    const a = qrMatrix(CARD_URL);
    const b = qrMatrix(CARD_URL);
    expect(qrPath(a, 4)).toBe(qrPath(b, 4));
  });
});

describe("qrPath", () => {
  it("offsets every module by the quiet zone", () => {
    const path = qrPath({ size: 2, isDark: (r, c) => r === c }, 4);
    expect(path).toBe("M4 4h1v1h-1zM5 5h1v1h-1z");
  });
});
