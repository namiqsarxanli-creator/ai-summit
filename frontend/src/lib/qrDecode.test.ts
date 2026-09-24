import { describe, expect, it } from "vitest";

import { encodeToImageData } from "@/test-utils/qr";

import { decodeQrFromImageData } from "./qrDecode";

const TOKEN = "GsQFmpQcGo84C7lq3NsnX7UA-DTaWZNbu-wres2bYlE";
const CARD_URL = `https://summit.example/c/${TOKEN}`;

describe("decodeQrFromImageData", () => {
  it("decodes a real FACEMARK card QR — the same M06 code an attendee shows", () => {
    const { data, width, height } = encodeToImageData(CARD_URL);
    expect(decodeQrFromImageData(data, width, height)).toBe(CARD_URL);
  });

  it("decodes an arbitrary external QR's payload too — validation is a separate step", () => {
    const { data, width, height } = encodeToImageData("https://example.com/not-facemark");
    expect(decodeQrFromImageData(data, width, height)).toBe("https://example.com/not-facemark");
  });

  it("returns null when no QR code is present in the frame", () => {
    const blank = new Uint8ClampedArray(200 * 200 * 4).fill(255);
    expect(decodeQrFromImageData(blank, 200, 200)).toBeNull();
  });
});
