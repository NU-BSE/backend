/**
 * `Buffer`, over `Uint8Array`.
 *
 * Node's Buffer *is* a Uint8Array subclass, so most of what servers do with it
 * — indexing, length, iteration, passing it somewhere that wants bytes — works
 * on the real thing already. What is missing in a browser is the constructors
 * and the encodings, which is what this adds.
 *
 * Not exhaustive: no pooling, no `readUInt32BE`, no slice-shares-memory
 * semantics beyond what Uint8Array gives. Those appear in code that parses
 * binary protocols, which is not what an MCP server bundled for a phone is
 * doing.
 */

function fromString(value, encoding = 'utf8') {
  if (encoding === 'base64') {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }
  if (encoding === 'hex') {
    const bytes = new Uint8Array(Math.floor(value.length / 2));
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = parseInt(value.substr(i * 2, 2), 16);
    }
    return bytes;
  }
  return new TextEncoder().encode(value);
}

function toString(bytes, encoding = 'utf8') {
  if (encoding === 'base64') {
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  if (encoding === 'hex') {
    let out = '';
    for (const byte of bytes) out += byte.toString(16).padStart(2, '0');
    return out;
  }
  return new TextDecoder().decode(bytes);
}

/**
 * Give a Uint8Array the Buffer methods callers expect.
 *
 * Defined per instance rather than by subclassing so that arrays produced
 * elsewhere — by `fetch`, by the fs shim — can be adopted without copying.
 */
function decorate(bytes) {
  Object.defineProperty(bytes, 'toString', {
    value: (encoding) => toString(bytes, encoding),
    configurable: true,
  });
  return bytes;
}

export const Buffer = {
  from(value, encoding) {
    if (typeof value === 'string') return decorate(fromString(value, encoding));
    if (value instanceof Uint8Array) return decorate(new Uint8Array(value));
    if (Array.isArray(value)) return decorate(new Uint8Array(value));
    if (value instanceof ArrayBuffer) return decorate(new Uint8Array(value));
    throw new TypeError('Buffer.from expects a string, array or ArrayBuffer.');
  },

  alloc(size, fill = 0) {
    const bytes = new Uint8Array(size);
    if (fill) bytes.fill(typeof fill === 'number' ? fill : 0);
    return decorate(bytes);
  },

  allocUnsafe(size) {
    return decorate(new Uint8Array(size));
  },

  concat(list, totalLength) {
    const length =
      totalLength ?? list.reduce((sum, item) => sum + item.length, 0);
    const out = new Uint8Array(length);
    let offset = 0;
    for (const item of list) {
      if (offset >= length) break;
      out.set(item.subarray(0, Math.min(item.length, length - offset)), offset);
      offset += item.length;
    }
    return decorate(out);
  },

  byteLength(value, encoding) {
    return typeof value === 'string' ? fromString(value, encoding).length : value.length;
  },

  isBuffer(value) {
    return value instanceof Uint8Array;
  },
};

export const constants = { MAX_LENGTH: 2 ** 31 - 1 };

export default { Buffer, constants };
