/**
 * `node:crypto`, reduced to what a bundled MCP server actually calls.
 *
 * This is the single most common blocker: of the official servers, three of
 * four need it and nothing else that is missing. What they need it for is
 * mundane — an id, a random token, a digest of a cache key — and none of it
 * requires the parts that would be dishonest to fake.
 *
 * Randomness comes from the platform's `crypto.getRandomValues`, which a
 * WebView has. It is a real CSPRNG, not `Math.random` dressed up: a server
 * generating a token with a predictable source would be worse than one that
 * failed to start.
 *
 * `createHash` is SHA-256 only, implemented here because the platform's
 * SubtleCrypto is asynchronous and `createHash(...).digest()` is not. Any
 * other algorithm throws by name rather than returning a wrong digest —
 * silently hashing with the wrong function is the failure mode worth
 * foreclosing, because nothing downstream would notice.
 */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function sha256(bytes) {
  const h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);

  // Pad to a multiple of 64 bytes: 0x80, zeros, then the length in bits as a
  // 64-bit big-endian integer.
  const length = bytes.length;
  const padded = new Uint8Array((((length + 8) >> 6) + 1) << 6);
  padded.set(bytes);
  padded[length] = 0x80;
  const bits = length * 8;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 4, bits >>> 0, false);
  view.setUint32(padded.length - 8, Math.floor(bits / 0x100000000), false);

  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));

  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let i = 0; i < 16; i += 1) w[i] = view.getUint32(offset + i * 4, false);
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i += 1) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      hh = g; g = f; f = e;
      e = (d + t1) >>> 0;
      d = c; c = b; b = a;
      a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0;
    h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0;
    h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
  }

  const digest = new Uint8Array(32);
  const out = new DataView(digest.buffer);
  for (let i = 0; i < 8; i += 1) out.setUint32(i * 4, h[i], false);
  return digest;
}

function toBytes(input, encoding) {
  if (typeof input === 'string') {
    if (encoding === 'hex') {
      const bytes = new Uint8Array(input.length / 2);
      for (let i = 0; i < bytes.length; i += 1) {
        bytes[i] = parseInt(input.substr(i * 2, 2), 16);
      }
      return bytes;
    }
    if (encoding === 'base64') {
      const binary = atob(input);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      return bytes;
    }
    return new TextEncoder().encode(input);
  }
  return input instanceof Uint8Array ? input : new Uint8Array(input);
}

function encode(bytes, encoding) {
  if (encoding === 'hex' || encoding === undefined) {
    let out = '';
    for (const byte of bytes) out += byte.toString(16).padStart(2, '0');
    return out;
  }
  if (encoding === 'base64') {
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  return new TextDecoder().decode(bytes);
}

class Hash {
  constructor(algorithm) {
    const normalised = String(algorithm).toLowerCase().replace('-', '');
    if (normalised !== 'sha256') {
      // Named, and refused. Returning a SHA-256 digest for a caller that asked
      // for MD5 would be accepted by everything and correct for nothing.
      throw new Error(
        `The sandbox implements createHash('sha256') only; '${algorithm}' was requested.`,
      );
    }
    this._chunks = [];
  }

  update(data, encoding) {
    this._chunks.push(toBytes(data, encoding));
    return this;
  }

  digest(encoding) {
    let total = 0;
    for (const chunk of this._chunks) total += chunk.length;
    const joined = new Uint8Array(total);
    let offset = 0;
    for (const chunk of this._chunks) {
      joined.set(chunk, offset);
      offset += chunk.length;
    }
    const digest = sha256(joined);
    return encoding ? encode(digest, encoding) : digest;
  }
}

export function createHash(algorithm) {
  return new Hash(algorithm);
}

export function randomBytes(size, callback) {
  const bytes = new Uint8Array(size);
  globalThis.crypto.getRandomValues(bytes);
  if (callback) {
    callback(null, bytes);
    return undefined;
  }
  return bytes;
}

export function randomUUID() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  // Version 4, from real randomness, for engines without randomUUID.
  const bytes = randomBytes(16);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = encode(bytes, 'hex');
  return [
    hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
    hex.slice(16, 20), hex.slice(20),
  ].join('-');
}

export function getRandomValues(array) {
  return globalThis.crypto.getRandomValues(array);
}

export function randomInt(min, max) {
  const [low, high] = max === undefined ? [0, min] : [min, max];
  const range = high - low;
  const bytes = randomBytes(4);
  const value = new DataView(bytes.buffer).getUint32(0, false);
  return low + (value % range);
}

/** Constant-time comparison, so a caller using it for a token keeps that. */
export function timingSafeEqual(a, b) {
  const left = toBytes(a);
  const right = toBytes(b);
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let i = 0; i < left.length; i += 1) difference |= left[i] ^ right[i];
  return difference === 0;
}

export const webcrypto = globalThis.crypto;

export default {
  createHash,
  randomBytes,
  randomUUID,
  getRandomValues,
  randomInt,
  timingSafeEqual,
  webcrypto,
};
