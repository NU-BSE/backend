/**
 * The parts of `node:util` that are pure functions.
 *
 * `promisify` and `callbackify` are adapters over calling conventions;
 * `TextEncoder`/`TextDecoder` are globals in a browser context; `inspect` is a
 * debug formatter. None of them touch the host, so none is a reason to refuse
 * a server.
 *
 * `inspect` is deliberately shallow rather than a faithful reimplementation of
 * Node's: it exists so a log line renders, and a server that depends on its
 * exact output for anything but logging has a problem this cannot fix.
 */

export function promisify(fn) {
  return (...args) =>
    new Promise((resolve, reject) => {
      fn(...args, (error, value) => (error ? reject(error) : resolve(value)));
    });
}

export function callbackify(fn) {
  return (...args) => {
    const callback = args.pop();
    Promise.resolve(fn(...args)).then(
      (value) => callback(null, value),
      (error) => callback(error),
    );
  };
}

export function inspect(value, options = {}) {
  const depth = options.depth ?? 2;
  const seen = new WeakSet();
  const render = (input, level) => {
    if (input === null) return 'null';
    if (typeof input === 'string') return level === 0 ? input : JSON.stringify(input);
    if (typeof input !== 'object') return String(input);
    if (seen.has(input)) return '[Circular]';
    if (level > depth) return Array.isArray(input) ? '[Array]' : '[Object]';
    seen.add(input);
    if (Array.isArray(input)) {
      return `[ ${input.map((item) => render(item, level + 1)).join(', ')} ]`;
    }
    const entries = Object.entries(input).map(
      ([key, item]) => `${key}: ${render(item, level + 1)}`,
    );
    return `{ ${entries.join(', ')} }`;
  };
  return render(value, 0);
}

export function format(...args) {
  if (typeof args[0] !== 'string') return args.map((arg) => inspect(arg)).join(' ');
  let index = 1;
  const formatted = args[0].replace(/%[sdifjoO%]/gu, (token) => {
    if (token === '%%') return '%';
    if (index >= args.length) return token;
    const value = args[index++];
    if (token === '%s') return String(value);
    if (token === '%d' || token === '%i') return String(parseInt(value, 10));
    if (token === '%f') return String(parseFloat(value));
    if (token === '%j') return JSON.stringify(value);
    return inspect(value);
  });
  return [formatted, ...args.slice(index).map((arg) => inspect(arg))].join(' ');
}

export function deprecate(fn) {
  return fn;
}

export const types = {
  isPromise: (value) => value instanceof Promise,
  isDate: (value) => value instanceof Date,
  isRegExp: (value) => value instanceof RegExp,
};

const TextEncoderCtor = globalThis.TextEncoder;
const TextDecoderCtor = globalThis.TextDecoder;
export { TextEncoderCtor as TextEncoder, TextDecoderCtor as TextDecoder };

export default {
  promisify,
  callbackify,
  inspect,
  format,
  deprecate,
  types,
  TextEncoder: TextEncoderCtor,
  TextDecoder: TextDecoderCtor,
};
