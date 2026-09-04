/**
 * POSIX `path`, in pure JavaScript.
 *
 * Refusing a server because it imports `node:path` would be wrong: this module
 * touches no filesystem, it manipulates strings. It is also unavoidable — the
 * official sequential-thinking server imports it, and so does most code that
 * has ever handled a filename.
 *
 * POSIX only, deliberately. The device is Android, so there are no drive
 * letters and no backslash separators to honour, and pretending to support
 * them would add branches that can never be exercised.
 */

function normalizeParts(parts, allowAboveRoot) {
  const out = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else if (allowAboveRoot) out.push('..');
    } else {
      out.push(part);
    }
  }
  return out;
}

export function normalize(input) {
  if (input.length === 0) return '.';
  const absolute = input.charCodeAt(0) === 47;
  const trailing = input.charCodeAt(input.length - 1) === 47;
  let joined = normalizeParts(input.split('/'), !absolute).join('/');
  if (!joined && !absolute) joined = '.';
  if (joined && trailing) joined += '/';
  return (absolute ? '/' : '') + joined;
}

export function join(...segments) {
  const joined = segments.filter((segment) => typeof segment === 'string' && segment).join('/');
  return joined ? normalize(joined) : '.';
}

export function resolve(...segments) {
  let resolved = '';
  let absolute = false;
  for (let i = segments.length - 1; i >= 0 && !absolute; i -= 1) {
    const segment = segments[i];
    if (typeof segment !== 'string' || !segment) continue;
    resolved = `${segment}/${resolved}`;
    absolute = segment.charCodeAt(0) === 47;
  }
  // There is no working directory in a sandbox; `/` is the only sensible base.
  if (!absolute) resolved = `/${resolved}`;
  const normalized = normalizeParts(resolved.split('/'), false).join('/');
  return `/${normalized}` || '/';
}

export function isAbsolute(input) {
  return input.length > 0 && input.charCodeAt(0) === 47;
}

export function dirname(input) {
  if (!input) return '.';
  const trimmed = input.replace(/\/+$/u, '');
  const index = trimmed.lastIndexOf('/');
  if (index < 0) return '.';
  if (index === 0) return '/';
  return trimmed.slice(0, index);
}

export function basename(input, extension) {
  const trimmed = String(input).replace(/\/+$/u, '');
  const name = trimmed.slice(trimmed.lastIndexOf('/') + 1);
  if (extension && name.endsWith(extension) && name !== extension) {
    return name.slice(0, -extension.length);
  }
  return name;
}

export function extname(input) {
  const name = basename(input);
  const index = name.lastIndexOf('.');
  return index <= 0 ? '' : name.slice(index);
}

export function relative(from, to) {
  const fromParts = resolve(from).split('/').filter(Boolean);
  const toParts = resolve(to).split('/').filter(Boolean);
  let shared = 0;
  while (
    shared < fromParts.length &&
    shared < toParts.length &&
    fromParts[shared] === toParts[shared]
  ) {
    shared += 1;
  }
  return [
    ...Array.from({ length: fromParts.length - shared }, () => '..'),
    ...toParts.slice(shared),
  ].join('/');
}

export function parse(input) {
  const base = basename(input);
  const ext = extname(input);
  return {
    root: isAbsolute(input) ? '/' : '',
    dir: dirname(input),
    base,
    ext,
    name: ext ? base.slice(0, -ext.length) : base,
  };
}

export function format(parts) {
  const base = parts.base || `${parts.name || ''}${parts.ext || ''}`;
  return parts.dir ? `${parts.dir === '/' ? '' : parts.dir}/${base}` : base;
}

export const sep = '/';
export const delimiter = ':';

const posix = {
  normalize, join, resolve, isAbsolute, dirname, basename, extname,
  relative, parse, format, sep, delimiter,
};
export { posix, posix as win32 };
export default { ...posix, posix, win32: posix };
