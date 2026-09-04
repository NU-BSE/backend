/**
 * A filesystem that lives in memory and is persisted by the host.
 *
 * Servers reach for `fs` constantly, and almost never for a filesystem. They
 * read their own package.json for a version string, and they keep a small
 * amount of state — saved locations, a cache, a memory graph — in one JSON
 * file under the home directory. Refusing every server that does either meant
 * refusing most of them, including the ones people actually want.
 *
 * The design is forced by one fact: `readFileSync` is synchronous, and the
 * bridge to the host is `postMessage`, which is not. A shim that awaited the
 * host could not implement the synchronous half of this module at all. So the
 * files live *here*, in the guest, as an ordinary object:
 *
 *   * reads are synchronous, from memory;
 *   * writes update memory synchronously and tell the host afterwards;
 *   * the host hands the previous contents back at startup, before the server
 *     runs, by defining `__CREEPY_FILES__`.
 *
 * What this is not: a real filesystem. There are no permissions, no streams,
 * no watching, and a server that expects to see files another program wrote
 * will find none. Those are absences a server can detect — `existsSync`
 * answers honestly — rather than corruptions it cannot.
 */

/*
 * Two sources, and the order matters. The seed is baked in by the bundler and
 * holds files that are part of the *server* — its package.json, which several
 * read for a version and refuse to start without. The host then supplies
 * whatever the server itself wrote on a previous run, which must win: a user's
 * saved state is not overwritten by the shipped default every launch.
 */
const seed = typeof __CREEPY_SEED_FILES__ !== 'undefined' ? __CREEPY_SEED_FILES__ : {};
const files = new Map(
  Object.entries({ ...seed, ...(globalThis.__CREEPY_FILES__ || {}) }),
);

/** Collapse `.` and `..`, so two spellings of a path are one key. */
function normalize(input) {
  const raw = String(input);
  const absolute = raw.startsWith('/');
  const parts = [];
  for (const part of raw.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return (absolute ? '/' : '') + parts.join('/');
}

// Writes are announced rather than awaited: nothing in the synchronous API can
// wait for the host, and a failure to persist must not fail the write the
// server already believes succeeded.
function persist() {
  globalThis.__creepyMcpHost?.saveFiles?.(Object.fromEntries(files));
}

function enoent(path, syscall) {
  const error = new Error(`ENOENT: no such file or directory, ${syscall} '${path}'`);
  error.code = 'ENOENT';
  error.errno = -2;
  error.path = path;
  error.syscall = syscall;
  return error;
}

export function existsSync(path) {
  const key = normalize(path);
  if (files.has(key)) return true;
  // A directory exists when something is inside it. Directories are not
  // tracked separately: mkdirSync creating an empty one that nothing can
  // observe would be bookkeeping with no reader.
  const prefix = `${key}/`;
  for (const name of files.keys()) if (name.startsWith(prefix)) return true;
  return false;
}

export function readFileSync(path, options) {
  const key = normalize(path);
  if (!files.has(key)) throw enoent(key, 'open');
  const content = files.get(key);
  const encoding = typeof options === 'string' ? options : options?.encoding;
  if (encoding) return content;
  // Callers that pass no encoding expect bytes.
  return new TextEncoder().encode(content);
}

export function writeFileSync(path, data) {
  const key = normalize(path);
  files.set(key, typeof data === 'string' ? data : new TextDecoder().decode(data));
  persist();
}

export function appendFileSync(path, data) {
  const key = normalize(path);
  const existing = files.get(key) ?? '';
  files.set(key, existing + (typeof data === 'string' ? data : new TextDecoder().decode(data)));
  persist();
}

export function mkdirSync() {
  // Directories are implied by the files in them; nothing to create.
  return undefined;
}

export function readdirSync(path) {
  const prefix = normalize(path) === '/' ? '/' : `${normalize(path)}/`;
  const names = new Set();
  for (const name of files.keys()) {
    if (!name.startsWith(prefix)) continue;
    const rest = name.slice(prefix.length);
    const slash = rest.indexOf('/');
    names.add(slash < 0 ? rest : rest.slice(0, slash));
  }
  return [...names].sort();
}

export function unlinkSync(path) {
  const key = normalize(path);
  if (!files.delete(key)) throw enoent(key, 'unlink');
  persist();
}

export function rmSync(path, options) {
  const key = normalize(path);
  const prefix = `${key}/`;
  let removed = files.delete(key);
  for (const name of [...files.keys()]) {
    if (name.startsWith(prefix)) {
      files.delete(name);
      removed = true;
    }
  }
  if (!removed && !options?.force) throw enoent(key, 'unlink');
  persist();
}

export function statSync(path) {
  const key = normalize(path);
  const isFile = files.has(key);
  if (!isFile && !existsSync(key)) throw enoent(key, 'stat');
  const size = isFile ? files.get(key).length : 0;
  return {
    size,
    isFile: () => isFile,
    isDirectory: () => !isFile,
    isSymbolicLink: () => false,
    mtime: new Date(0),
    mtimeMs: 0,
  };
}

export function renameSync(from, to) {
  const source = normalize(from);
  if (!files.has(source)) throw enoent(source, 'rename');
  files.set(normalize(to), files.get(source));
  files.delete(source);
  persist();
}

export function copyFileSync(from, to) {
  const source = normalize(from);
  if (!files.has(source)) throw enoent(source, 'copyfile');
  files.set(normalize(to), files.get(source));
  persist();
}

/**
 * Streams over an in-memory file.
 *
 * The content is already whole by the time anything asks for it, so the stream
 * emits one chunk and ends. That differs from Node only in chunk boundaries,
 * which no correct consumer depends on — a reader that concatenates `data`
 * events gets exactly the same bytes.
 */
class FileStream {
  constructor() {
    this._handlers = new Map();
  }

  on(event, handler) {
    const handlers = this._handlers.get(event) ?? [];
    handlers.push(handler);
    this._handlers.set(event, handlers);
    return this;
  }

  once(event, handler) {
    return this.on(event, handler);
  }

  emit(event, ...args) {
    for (const handler of this._handlers.get(event) ?? []) handler(...args);
    return this;
  }

  setEncoding(encoding) {
    this._encoding = encoding;
    return this;
  }

  pipe(destination) {
    this.on('data', (chunk) => destination.write?.(chunk));
    this.on('end', () => destination.end?.());
    return destination;
  }

  destroy() {
    return this;
  }
}

export function createReadStream(path, options) {
  const stream = new FileStream();
  const encoding = typeof options === 'string' ? options : options?.encoding;
  // Deferred so a caller can attach handlers before anything is emitted, as it
  // would with a real stream.
  Promise.resolve().then(() => {
    try {
      stream.emit('data', readFileSync(path, encoding ? { encoding } : undefined));
      stream.emit('end');
      stream.emit('close');
    } catch (error) {
      stream.emit('error', error);
    }
  });
  return stream;
}

export function createWriteStream(path) {
  const stream = new FileStream();
  const chunks = [];
  stream.write = (chunk) => {
    chunks.push(typeof chunk === 'string' ? chunk : new TextDecoder().decode(chunk));
    return true;
  };
  stream.end = (chunk) => {
    if (chunk) stream.write(chunk);
    try {
      writeFileSync(path, chunks.join(''));
      stream.emit('finish');
      stream.emit('close');
    } catch (error) {
      stream.emit('error', error);
    }
    return stream;
  };
  return stream;
}

/** The callback API, over the same store. */
function callbackify(fn) {
  return (...args) => {
    const callback = typeof args[args.length - 1] === 'function' ? args.pop() : null;
    try {
      const value = fn(...args);
      if (callback) callback(null, value);
    } catch (error) {
      if (callback) callback(error);
      else throw error;
    }
  };
}

export const readFile = callbackify(readFileSync);
export const writeFile = callbackify(writeFileSync);
export const mkdir = callbackify(mkdirSync);
export const readdir = callbackify(readdirSync);
export const unlink = callbackify(unlinkSync);
export const stat = callbackify(statSync);

export const promises = {
  readFile: async (...args) => readFileSync(...args),
  writeFile: async (...args) => writeFileSync(...args),
  appendFile: async (...args) => appendFileSync(...args),
  mkdir: async (...args) => mkdirSync(...args),
  readdir: async (...args) => readdirSync(...args),
  unlink: async (...args) => unlinkSync(...args),
  rm: async (...args) => rmSync(...args),
  stat: async (...args) => statSync(...args),
  rename: async (...args) => renameSync(...args),
  copyFile: async (...args) => copyFileSync(...args),
  access: async (path) => {
    if (!existsSync(path)) throw enoent(normalize(path), 'access');
  },
};

export const constants = { F_OK: 0, R_OK: 4, W_OK: 2, X_OK: 1 };

export default {
  existsSync, readFileSync, writeFileSync, appendFileSync, mkdirSync,
  readdirSync, unlinkSync, rmSync, statSync, renameSync, copyFileSync,
  createReadStream, createWriteStream,
  readFile, writeFile, mkdir, readdir, unlink, stat,
  promises, constants,
};
