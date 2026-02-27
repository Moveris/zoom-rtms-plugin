import crypto from "node:crypto";

export interface ZoomContext {
  typ: string;
  uid: string;
  mid: string;
  ts: number;
}

/**
 * Decrypt the x-zoom-app-context header (or getAppContext() value).
 *
 * Buffer format (per Zoom's official reference implementation):
 *   [ivLength: 1 byte][iv][aadLength: 2 bytes LE][aad][cipherLength: 4 bytes LE][cipherText][tag: 16 bytes]
 *
 * Key derivation: SHA-256 hash of the Zoom Client Secret.
 * Cipher: AES-256-GCM with AAD, auth tag length 16, no padding.
 *
 * Reference: https://github.com/zoom/zoomapps-advancedsample-react/blob/main/backend/util/zoom-helpers.js
 */
export function decryptZoomContext(context: string, clientSecret: string): ZoomContext {
  let buf = Buffer.from(context, "base64");
  if (buf.length < 24) {
    throw new Error("Invalid zoom context: too short");
  }

  // 1. Read IV length (1 byte) and IV
  const ivLength = buf.readUInt8(0);
  buf = buf.subarray(1);
  const iv = buf.subarray(0, ivLength);
  buf = buf.subarray(ivLength);

  // 2. Read AAD length (2 bytes, little-endian) and AAD
  const aadLength = buf.readUInt16LE(0);
  buf = buf.subarray(2);
  const aad = buf.subarray(0, aadLength);
  buf = buf.subarray(aadLength);

  // 3. Read cipher text length (4 bytes, little-endian) and cipher text
  const cipherLength = buf.readInt32LE(0);
  buf = buf.subarray(4);
  const cipherText = buf.subarray(0, cipherLength);

  // 4. Auth tag is the remaining bytes after cipher text (16 bytes)
  const tag = buf.subarray(cipherLength);

  // 5. Derive key from client secret via SHA-256
  const key = crypto.createHash("sha256").update(clientSecret).digest();

  // 6. Decrypt
  const decipher = crypto
    .createDecipheriv("aes-256-gcm", key, iv, { authTagLength: 16 })
    .setAAD(aad)
    .setAuthTag(tag)
    .setAutoPadding(false);

  const decrypted = decipher.update(cipherText, undefined, "utf-8") + decipher.final("utf-8");
  const payload = JSON.parse(decrypted);

  return {
    typ: payload.typ,
    uid: payload.uid,
    mid: payload.mid ?? "",
    ts: payload.ts,
  };
}
