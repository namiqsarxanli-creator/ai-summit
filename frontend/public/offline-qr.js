/* Offline QR stores only an encrypted QR drawing. No session, profile or points are cached. */
(() => {
  const KEY = "facemark.offlineQr.v1";
  const ITERATIONS = 250000;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function base64(bytes) {
    let result = "";
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      result += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    }
    return btoa(result);
  }

  function unbase64(value) {
    return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
  }

  function validDrawing(drawing) {
    return drawing && Number.isInteger(drawing.dim) &&
      drawing.dim >= 29 && drawing.dim <= 185 &&
      typeof drawing.path === "string" &&
      drawing.path.length > 0 && drawing.path.length < 250000 &&
      /^[M0-9 hvz-]+$/.test(drawing.path);
  }

  async function keyFromPin(pin, salt) {
    if (!/^\d{6,12}$/.test(pin)) throw new Error("PIN must contain 6 to 12 digits");
    const material = await crypto.subtle.importKey("raw", encoder.encode(pin), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      { name: "PBKDF2", salt, iterations: ITERATIONS, hash: "SHA-256" },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
  }

  async function save(pin, drawing) {
    if (!validDrawing(drawing)) throw new Error("Invalid QR drawing");
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await keyFromPin(pin, salt);
    const ciphertext = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv }, key, encoder.encode(JSON.stringify(drawing)),
    );
    localStorage.setItem(KEY, JSON.stringify({
      v: 1, salt: base64(salt), iv: base64(iv), ciphertext: base64(new Uint8Array(ciphertext)),
    }));
  }

  async function unlock(pin) {
    const raw = localStorage.getItem(KEY);
    if (!raw) throw new Error("No offline QR is saved");
    try {
      const record = JSON.parse(raw);
      if (record.v !== 1) throw new Error("Unsupported offline QR");
      const salt = unbase64(record.salt);
      const iv = unbase64(record.iv);
      if (salt.length !== 16 || iv.length !== 12) throw new Error("Invalid offline QR");
      const key = await keyFromPin(pin, salt);
      const plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv }, key, unbase64(record.ciphertext),
      );
      const drawing = JSON.parse(decoder.decode(plain));
      if (!validDrawing(drawing)) throw new Error("Invalid offline QR");
      return drawing;
    } catch {
      throw new Error("PIN incorrect or offline QR unavailable");
    }
  }

  window.FacemarkOfflineQr = Object.freeze({
    save,
    unlock,
    hasCopy: () => localStorage.getItem(KEY) !== null,
    clear: () => localStorage.removeItem(KEY),
  });
})();