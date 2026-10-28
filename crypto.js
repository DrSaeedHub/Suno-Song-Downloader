/**
 * Song decryption, mirroring Suno's web player:
 *
 *   * The media file is AES-128-CTR encrypted under a per-song content key,
 *     with a 16-byte base IV as the initial counter block.
 *   * POST /api/mango/rights returns that content key and base IV, each
 *     AES-256-GCM "wrapped" and base64 encoded (fields "key" / "iv").
 *   * The unwrap key (KEK) is SHA-256 of a secret: the access token sent on
 *     the rights request, or - for some licenses - the short-lived "glt"
 *     rights token returned alongside. The GCM additional data is the clip id.
 *
 *   secret -> SHA-256 -> KEK --(AES-GCM, aad=clip id)--> content key + base IV
 *   file --(AES-128-CTR, key=content key, counter=base IV)--> Opus/MP4 audio
 */

const encoder = new TextEncoder();

export function base64ToBytes(base64) {
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

async function kekFrom(secret) {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(secret));
  return crypto.subtle.importKey('raw', digest, { name: 'AES-GCM' }, false, ['decrypt']);
}

// AES-GCM open: nonce = first 12 bytes, tag appended to the ciphertext.
async function gcmOpen(blob, aad, kek) {
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: blob.slice(0, 12), additionalData: encoder.encode(aad) },
    kek,
    blob.slice(12),
  );
  return new Uint8Array(plain);
}

/**
 * Unwrap a rights response into { key, iv } for one clip. `secrets` are the
 * candidate KEK sources (access token, glt); each is tried in turn, since a
 * wrong one fails GCM authentication cleanly.
 */
export async function unwrapLicense(license, clipId, secrets) {
  if (!license?.key || !license?.iv) throw new Error('The license response did not contain key/iv');
  const wrappedKey = base64ToBytes(license.key);
  const wrappedIv = base64ToBytes(license.iv);
  for (const secret of new Set(secrets.filter(Boolean))) {
    const kek = await kekFrom(secret);
    try {
      const key = await gcmOpen(wrappedKey, clipId, kek);
      const iv = await gcmOpen(wrappedIv, clipId, kek);
      if (iv.length !== 16) throw new Error(`Unexpected base IV length: ${iv.length}`);
      return { key, iv };
    } catch (error) {
      if (error?.name !== 'OperationError') throw error; // OperationError = wrong KEK
    }
  }
  throw new Error('Could not unwrap the song key (wrong or expired token)');
}

export async function decryptMedia(ciphertext, key, iv) {
  const ctrKey = await crypto.subtle.importKey('raw', key, { name: 'AES-CTR' }, false, ['decrypt']);
  const plain = await crypto.subtle.decrypt({ name: 'AES-CTR', counter: iv, length: 128 }, ctrKey, ciphertext);
  return new Uint8Array(plain);
}

/** Mirror the player's magic sniffing: WebM (EBML) vs MP4 (ftyp). */
export function sniffExtension(data) {
  if (data.length >= 4 && data[0] === 0x1a && data[1] === 0x45 && data[2] === 0xdf && data[3] === 0xa3) return 'webm';
  if (data.length >= 8 && data[4] === 0x66 && data[5] === 0x74 && data[6] === 0x79 && data[7] === 0x70) return 'm4a';
  return 'bin';
}
